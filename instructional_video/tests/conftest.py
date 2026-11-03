from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def demo_script() -> dict:
    return json.loads((FIXTURES / "demo_script.json").read_text())


@pytest.fixture
def project(tmp_path, demo_script):
    """A project with script.json and silence-engine timing (no TTS model needed)."""
    from ivideo.project import Project, write_json
    from ivideo.schema import assign_scene_ids
    from ivideo.tts import synthesize

    proj = Project(tmp_path / "proj").ensure()
    script = assign_scene_ids(demo_script)
    write_json(proj.script, script)
    synthesize(proj, script, engine="silence")
    return proj


def copy_scene(project, name: str, sid: str) -> Path:
    dst = project.scene_file(sid)
    shutil.copy(FIXTURES / name, dst)
    return dst
