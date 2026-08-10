"""Fixtures for the D8 E-series.

Three tiers of availability, and the split matters:

- **Always runs.** E1 (capacity) and E4 (PTS honesty) are pure Python and
  need nothing but the repo. E2's byte comparison needs the committed
  ``observations.swob``/``packets.hex``, which are in the repo too.
- **Needs a C toolchain.** Anything that invokes the daemon or the fixture
  tool builds them on demand and SKIPS when cmake/cc are absent. The skip is
  loud in the report, never silent, and the committed byte fixtures still
  gate the Python side on such a machine.
- **Needs machine-local render artifacts.** The gate fixture's clip lives
  under ``output/`` and is not in the repo; regeneration skips without it,
  but the DERIVED gate fixture is committed and its byte gate always runs.

The same rule the W-series set: "all E tests skipped" must never be
mistakable for "all E tests passed", so every axis has a variant that runs
everywhere.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from skyweave2.contracts import Observation2D
from skyweave2.detector.config import DetectorConfig
from skyweave2.edge import daemon, fixtures
from skyweave2.edge.obsfixture import decode_fixture

FIXTURE_ROOT = fixtures.FIXTURE_ROOT
COMMITTED_FIXTURES = ("sparse", "clutter", "gate")
# The fixtures whose clips this suite can regenerate from a seed. The gate
# fixture's clip is a machine-local render artifact and is not one of them.
REPLAYABLE_FIXTURES = tuple(fixtures.SYNTHETIC_FIXTURES)


def fixture_dir(name: str) -> Path:
    return FIXTURE_ROOT / name


def load_config(name: str) -> DetectorConfig:
    return DetectorConfig.model_validate(
        json.loads((fixture_dir(name) / "config.json").read_text())
    )


def load_observations(name: str) -> list[Observation2D]:
    raw = (fixture_dir(name) / "observations.swob").read_bytes()
    return [obs for _, event in decode_fixture(io.BytesIO(raw)) for obs in event]


def load_events(name: str):
    raw = (fixture_dir(name) / "observations.swob").read_bytes()
    return decode_fixture(io.BytesIO(raw))


def load_packets(name: str) -> list[bytes]:
    text = (fixture_dir(name) / "packets.hex").read_text(encoding="utf-8")
    return [bytes.fromhex(line) for line in text.split() if line]


def load_stats(name: str) -> dict:
    return json.loads((fixture_dir(name) / "stats.json").read_text())


@pytest.fixture(scope="session")
def edge_build_dir(tmp_path_factory) -> Path:
    """Build the C daemon and the fixture tool once per session.

    Built into a session tmp dir rather than the checked-in ``build-host``:
    a test must not depend on, or quietly repair, whatever the developer last
    left in their build tree.
    """
    if not daemon.host_toolchain_available():
        pytest.skip("no cmake/cc on this machine; the C daemon cannot be built")
    build = tmp_path_factory.mktemp("skyweave-edge-build")
    configure = subprocess.run(
        ["cmake", "-S", str(daemon.FIRMWARE_ROOT), "-B", str(build),
         "-DCMAKE_BUILD_TYPE=Release"],
        capture_output=True, text=True, check=False,
    )
    # A configure failure is NOT an environment problem to shrug at. The
    # toolchain check above already covers "this machine has no compiler";
    # anything that fails after that is a broken CMakeLists, a missing
    # source, or a bad option — a defect that would otherwise skip 30+ tests
    # and let the suite exit 0 while the daemon did not exist.
    assert configure.returncode == 0, (
        f"cmake configure failed:\n{configure.stdout}\n{configure.stderr}"
    )
    compile_step = subprocess.run(
        ["cmake", "--build", str(build), "-j", "4"],
        capture_output=True, text=True, check=False,
    )
    assert compile_step.returncode == 0, (
        f"the C daemon failed to compile:\n{compile_step.stdout}\n{compile_step.stderr}"
    )
    # The build must have produced BOTH binaries. A configure+build that
    # succeeds while emitting nothing is the failure mode a returncode check
    # cannot see.
    for binary in ("skyweave-edge", "sw-fixture-tool"):
        assert (build / binary).exists(), f"{binary} was not produced by the build"
    return build


@pytest.fixture(scope="session")
def scene_clips(tmp_path_factory) -> dict:
    """Regenerate the synthetic fixture clips from their declared seeds."""
    root = tmp_path_factory.mktemp("skyweave-edge-clips")
    return {
        name: fixtures.build_scene_clip(root / name, blobs)
        for name, blobs in fixtures.SYNTHETIC_FIXTURES.items()
    }
