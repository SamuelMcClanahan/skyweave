"""Project folder layout. One folder per video under instructional_video/projects/<slug>/."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROJECTS = PKG_ROOT / "projects"
MODELS_DIR = PKG_ROOT / "models"


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len].rstrip("-")) or "video"


@dataclass(frozen=True)
class Project:
    root: Path

    @classmethod
    def open(cls, path_or_slug: str | Path) -> Project:
        p = Path(path_or_slug)
        if not p.is_absolute() and len(p.parts) == 1:
            p = DEFAULT_PROJECTS / p
        return cls(p.resolve())

    # files
    @property
    def brief(self) -> Path:
        return self.root / "brief.json"

    @property
    def script(self) -> Path:
        return self.root / "script.json"

    @property
    def openrouter_log(self) -> Path:
        return self.root / "openrouter_response.json"

    @property
    def timing(self) -> Path:
        return self.root / "timing.json"

    @property
    def status_file(self) -> Path:
        return self.root / "status.json"

    # folders
    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def scenes_dir(self) -> Path:
        return self.root / "scenes"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def frames_dir(self) -> Path:
        return self.root / "frames"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    def scene_file(self, sid: str) -> Path:
        return self.scenes_dir / f"{sid}.py"

    def render_file(self, sid: str, quality: str) -> Path:
        return self.renders_dir / quality / f"{sid}.mp4"

    def report_file(self, sid: str, quality: str) -> Path:
        return self.renders_dir / quality / f"{sid}.report.json"

    def ensure(self) -> Project:
        for d in (
            self.root,
            self.audio_dir,
            self.scenes_dir,
            self.renders_dir,
            self.frames_dir,
            self.out_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # status
    def load_status(self) -> dict:
        if self.status_file.exists():
            return json.loads(self.status_file.read_text())
        return {"scenes": {}}

    def set_scene_state(self, sid: str, state: str, note: str = "") -> dict:
        st = self.load_status()
        st.setdefault("scenes", {})[sid] = {"state": state, "note": note}
        write_json(self.status_file, st)
        return st


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(Path(path).read_text())
