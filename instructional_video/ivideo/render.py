"""Render one scene file with Manim, then extract check frames.

Quality presets:
  draft: 854x480 @ 15 fps (fast, for the write-check-fix loop)
  final: 1920x1080 @ 30 fps (for assembly)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .project import Project, read_json

QUALITY = {
    "draft": {"res": "854,480", "fps": 15},
    "final": {"res": "1920,1080", "fps": 30},
}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return float(out)


def _scene_timing(project: Project, sid: str) -> dict:
    for sc in read_json(project.timing)["scenes"]:
        if sc["id"] == sid:
            return sc
    raise SystemExit(f"scene {sid} not in {project.timing}")


def _tail(text: str, n: int = 60) -> str:
    lines = _ANSI.sub("", text).splitlines()
    # Keep the traceback / error block; drop progress bars.
    lines = [ln for ln in lines if "it/s]" not in ln and "Animation " not in ln[:12]]
    return "\n".join(lines[-n:])


def render_scene(
    project: Project, sid: str, quality: str = "draft", frames: bool = True, timeout: int = 1800
) -> dict:
    """Render scenes/<sid>.py (class Main). Returns a result dict and writes the report JSON."""
    if quality not in QUALITY:
        raise SystemExit(f"quality must be one of {list(QUALITY)}")
    src = project.scene_file(sid)
    if not src.exists():
        raise SystemExit(f"{src} does not exist")
    q = QUALITY[quality]
    out = project.render_file(sid, quality)
    report = project.report_file(sid, quality)
    out.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    media = project.root / ".media" / quality / sid
    shutil.rmtree(media, ignore_errors=True)
    env = dict(os.environ)
    env.update(
        IVIDEO_TIMING=str(project.timing),
        IVIDEO_SCENE=sid,
        IVIDEO_REPORT=str(report),
        PYTHONWARNINGS="ignore",
        # scenes/common.py (the per-video drawing library) is importable as `common`.
        PYTHONPATH=os.pathsep.join(
            x for x in (str(project.scenes_dir), os.environ.get("PYTHONPATH", "")) if x
        ),
    )
    cmd = [
        sys.executable, "-m", "manim", "render",
        "--resolution", q["res"], "--frame_rate", str(q["fps"]),
        "--media_dir", str(media), "--disable_caching", "--progress_bar", "none",
        "-o", sid, str(src), "Main",
    ]  # fmt: skip
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout, cwd=project.root
        )
    except subprocess.TimeoutExpired:
        return {"scene": sid, "ok": False, "error": f"render timed out after {timeout}s"}
    elapsed = round(time.time() - t0, 1)
    log = project.renders_dir / quality / f"{sid}.log"
    log.write_text(_ANSI.sub("", proc.stdout + "\n" + proc.stderr))
    produced = sorted(media.rglob(f"{sid}.mp4"))
    if proc.returncode != 0 or not produced:
        return {
            "scene": sid,
            "ok": False,
            "error": "manim failed",
            "returncode": proc.returncode,
            "log_tail": _tail(proc.stderr or proc.stdout),
            "log": str(log),
        }
    shutil.move(str(produced[0]), out)
    shutil.rmtree(media, ignore_errors=True)
    result = {
        "scene": sid,
        "ok": True,
        "quality": quality,
        "video": str(out),
        "seconds_to_render": elapsed,
        "video_duration": round(probe_duration(out), 3),
    }
    if report.exists():
        rep = json.loads(report.read_text())
        result.update(
            target_duration=rep["target_duration"],
            duration_error=rep["duration_error"],
            drift=rep["drift"],
            layout_issues=rep["layout_issues"],
            clean=rep["clean"],
        )
    else:
        result.update(
            clean=False,
            layout_issues=[],
            drift=[{"why": "no report: is the class a SyncedScene that calls finish()?"}],
        )
    if frames:
        result["frames"] = extract_frames(project, sid, quality)
    return result


def extract_frames(project: Project, sid: str, quality: str = "draft") -> dict:
    """One PNG per line (at 70% through the line) plus the last frame, and a contact sheet."""
    sc = _scene_timing(project, sid)
    video = project.render_file(sid, quality)
    vdur = probe_duration(video)
    fdir = project.frames_dir / quality / sid
    shutil.rmtree(fdir, ignore_errors=True)
    fdir.mkdir(parents=True, exist_ok=True)
    shots = []
    for ln in sc["lines"]:
        t = min(vdur - 0.25, ln["start"] + 0.7 * (ln["end"] - ln["start"]))
        shots.append(
            (f"line_{ln['index']:02d}", t, f"L{ln['index']} {ln['speaker']}: {ln['text']}")
        )
    shots.append(("end", max(0.0, vdur - 0.25), "last frame"))
    paths = []
    for name, t, caption in shots:
        p = fdir / f"{name}.png"
        grab_frame(video, max(0.0, t), p)
        paths.append({"name": name, "t": round(t, 2), "path": str(p), "caption": caption})
    sheet = fdir / "contact_sheet.png"
    contact_sheet(
        [Path(x["path"]) for x in paths], [f"t={x['t']:.1f}s  {x['caption']}" for x in paths], sheet
    )
    # Frame 0, for checking the cut from the previous scene (not on the contact sheet).
    start = grab_frame(video, 0.0, fdir / "start.png")
    return {
        "dir": str(fdir),
        "frames": paths,
        "contact_sheet": str(sheet),
        "start_frame": str(start),
        "end_frame": str(fdir / "end.png"),
    }


def render_still(project: Project, file: str, cls: str = "Gallery", timeout: int = 600) -> dict:
    """Render the last frame of a plain Scene class (for example the drawing-library gallery)."""
    src = Path(file)
    if not src.is_absolute():
        src = (project.scenes_dir / src) if (project.scenes_dir / src).exists() else src.resolve()
    if not src.exists():
        raise SystemExit(f"{src} does not exist")
    media = project.root / ".media" / "still" / src.stem
    shutil.rmtree(media, ignore_errors=True)
    env = dict(os.environ)
    env.update(
        PYTHONWARNINGS="ignore",
        PYTHONPATH=os.pathsep.join(
            x for x in (str(project.scenes_dir), os.environ.get("PYTHONPATH", "")) if x
        ),
    )
    cmd = [
        sys.executable, "-m", "manim", "render", "-s",
        "--resolution", "1280,720", "--media_dir", str(media),
        "--disable_caching", "--progress_bar", "none", "-o", src.stem, str(src), cls,
    ]  # fmt: skip
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout, cwd=project.root
    )
    pngs = sorted(media.rglob("*.png"))
    if proc.returncode != 0 or not pngs:
        return {"ok": False, "error": "manim failed", "log_tail": _tail(proc.stderr or proc.stdout)}
    out = project.frames_dir / "still" / f"{src.stem}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pngs[-1]), out)
    shutil.rmtree(media, ignore_errors=True)
    return {"ok": True, "image": str(out)}


def grab_frame(video: Path, t: float, out: Path) -> Path:
    """Write the frame at time t. Falls back to the last frame when t is past the end."""
    out.unlink(missing_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(out),
        ],
        check=True,
    )
    if not out.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-sseof",
                "-0.5",
                "-i",
                str(video),
                "-update",
                "1",
                "-q:v",
                "2",
                str(out),
            ],
            check=True,
        )
    if not out.exists():
        raise RuntimeError(f"could not extract a frame at {t:.2f}s from {video}")
    return out


def contact_sheet(
    images: list[Path], captions: list[str], out: Path, cols: int = 2, width: int = 640
) -> Path:
    ims = [Image.open(p).convert("RGB") for p in images]
    if not ims:
        raise ValueError("no images")
    h = int(width * ims[0].height / ims[0].width)
    cap_h = 44
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * width, rows * (h + cap_h)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    for i, (im, cap) in enumerate(zip(ims, captions, strict=True)):
        x, y = (i % cols) * width, (i // cols) * (h + cap_h)
        sheet.paste(im.resize((width, h)), (x, y + cap_h))
        rows_txt = textwrap.wrap(
            cap, width=max(20, int(width / 7.6)), max_lines=2, placeholder="..."
        )
        for k, row in enumerate(rows_txt):
            draw.text((x + 6, y + 4 + 18 * k), row, fill=(235, 235, 235), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out
