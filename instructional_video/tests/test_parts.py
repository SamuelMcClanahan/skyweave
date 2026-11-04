"""common.py loads finished part files and skips broken ones."""

from __future__ import annotations

import subprocess
import sys


def test_load_parts_skips_broken(tmp_path):
    (tmp_path / "spec.py").write_text("POS = (1, 2)\n")
    (tmp_path / "parts_s01.py").write_text("from spec import *\ndef cam():\n    return POS\n")
    (tmp_path / "parts_s02.py").write_text("def half_written(:\n")  # a builder mid-write
    (tmp_path / "common.py").write_text(
        "from spec import *\nfrom ivideo.parts import load_parts\nLOADED = load_parts(globals())\n"
        "def handoff_s01_s02():\n    return [cam()]\n"
    )
    code = "import common; print(common.LOADED, common.handoff_s01_s02())"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr
    assert "['parts_s01'] [(1, 2)]" in out.stdout
    assert "skipped parts_s02.py" in out.stderr
