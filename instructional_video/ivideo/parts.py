"""Load the per-scene drawing-part files of a video into common.py's namespace.

The Design step writes scenes/spec.py (positions, colors, sizes) and scenes/common.py.
Part builders write scenes/parts_<scene>.py in parallel. common.py calls
load_parts(globals()) to import every parts_*.py next to it. A part file that is
still being written (or broken) is skipped with a warning, so a scene whose own
parts are finished can render while other builders are still working.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_parts(namespace: dict) -> list[str]:
    here = Path(namespace.get("__file__", ".")).resolve().parent
    loaded = []
    for f in sorted(here.glob("parts_*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f.stem, f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f.stem] = mod
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001 - report and keep going
            print(f"[ivideo] skipped {f.name}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        names = getattr(mod, "__all__", None) or [
            n for n in vars(mod) if not n.startswith("_") and n != "Gallery"
        ]
        for n in names:
            namespace[n] = vars(mod)[n]
        loaded.append(f.stem)
    return loaded
