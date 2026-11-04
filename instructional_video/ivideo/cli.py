"""ivideo command line. Every command prints JSON on stdout so agents can parse it.

ivideo doctor
ivideo new <project> --topic "..." [--audience ..] [--minutes 3] [--speakers 2]
ivideo script-rules <project>              # rules + schema; a Claude agent writes script.json
ivideo check-script <project>              # validate script.json
ivideo dialogue <project> [--model id] ...  # optional: script from an OpenRouter LLM instead
ivideo voice <project> [--engine openrouter|kokoro|tone] [--tts-model id] [--voice host=nadine]
ivideo brief <project> [<scene>]          # scene data an animator needs
ivideo render <project> <scene> [--quality draft|final] [--no-frames]
ivideo render-all <project> [--quality final]
ivideo frames <project> <scene> [--quality draft]
ivideo still <project> <file.py> [--class Gallery]   # last frame of a plain Scene
ivideo mark <project> <scene> <state> [--note ..]
ivideo assemble <project> [--burn-subtitles]
ivideo status <project>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .project import MODELS_DIR, Project, read_json, slugify, write_json

STATES = ("todo", "written", "rendered", "approved", "failed")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_doctor(_a) -> dict:
    def has_mod(name: str) -> bool:
        import importlib.util

        return importlib.util.find_spec(name) is not None

    checks = {
        "python": sys.version.split()[0],
        "manim": has_mod("manim"),
        "kokoro_onnx": has_mod("kokoro_onnx"),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "kokoro_model": (MODELS_DIR / "kokoro-v1.0.onnx").exists()
        and (MODELS_DIR / "voices-v1.0.bin").exists(),
        "latex": shutil.which("latex") is not None and shutil.which("dvisvgm") is not None,
        "openrouter_key": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
    }
    fonts = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    checks["font_dejavu"] = "DejaVu Sans" in fonts
    required = ["manim", "kokoro_onnx", "ffmpeg", "ffprobe", "kokoro_model", "font_dejavu"]
    checks["ready_for_render"] = all(checks[k] for k in required)
    checks["ready_for_voice"] = checks["openrouter_key"]  # default voice engine is OpenRouter
    checks["notes"] = []
    if not checks["latex"]:
        checks["notes"].append(
            "No LaTeX: scenes must not use MathTex/Tex. Use Text. Run ./setup.sh --latex to enable."
        )
    if not checks["openrouter_key"]:
        checks["notes"].append(
            "OPENROUTER_API_KEY is not set: the OpenRouter voice cannot run (use --engine kokoro to voice locally)."
        )
    else:
        from . import openrouter

        try:
            k = openrouter.key_status()
            checks["openrouter_credit"] = {
                x: k.get(x) for x in ("limit", "limit_remaining", "usage", "is_free_tier")
            }
        except Exception as e:  # report, do not fail doctor
            checks["notes"].append(f"OpenRouter key check failed: {e}")
    return checks


def cmd_new(a) -> dict:
    proj = Project.open(a.project or slugify(a.topic)).ensure()
    brief = {
        "topic": a.topic,
        "audience": a.audience,
        "minutes": a.minutes,
        "speakers": a.speakers,
        "notes": a.notes or "",
        "sources": [str(Path(s).resolve()) for s in (a.source or [])],
    }
    write_json(proj.brief, brief)
    return {"project": str(proj.root), "brief": brief}


def cmd_script_rules(a) -> dict:
    """The rules and JSON shape for a script written by a Claude agent."""
    from . import dialogue
    from .schema import SCRIPT_SCHEMA

    proj = Project.open(a.project)
    brief = read_json(proj.brief) if proj.brief.exists() else {}
    minutes = float(brief.get("minutes", 3.0))
    return {
        "write_to": str(proj.script),
        "brief": brief,
        "target_words": int(minutes * dialogue.WORDS_PER_MINUTE),
        "rules": dialogue.SYSTEM_PROMPT,
        "speaker_mode": dialogue.speaker_mode(int(brief.get("speakers", 2))),
        "json_schema": SCRIPT_SCHEMA,
    }


def cmd_check_script(a) -> dict:
    """Validate script.json, give scenes ids s01.., and add word counts."""
    from . import dialogue
    from .schema import assign_scene_ids, total_words, validate_script

    proj = Project.open(a.project)
    data = read_json(proj.script)
    brief = read_json(proj.brief) if proj.brief.exists() else {}
    problems = validate_script(data, speakers=brief.get("speakers"))
    if problems:
        return {"ok": False, "problems": problems}
    data = assign_scene_ids(data)
    words = total_words(data)
    data["meta"] = {
        **data.get("meta", {}),
        "topic": brief.get("topic", data.get("meta", {}).get("topic", "")),
        "script_writer": a.writer,
        "words": words,
        "estimated_minutes": round(words / dialogue.WORDS_PER_MINUTE, 2),
    }
    write_json(proj.script, data)
    return {
        "ok": True,
        "words": words,
        "estimated_minutes": data["meta"]["estimated_minutes"],
        "scenes": [
            {"id": s["id"], "title": s["title"], "lines": len(s["lines"])} for s in data["scenes"]
        ],
    }


def cmd_dialogue(a) -> dict:
    from . import dialogue

    proj = Project.open(a.project).ensure()
    brief = read_json(proj.brief) if proj.brief.exists() else {}
    topic = a.topic or brief.get("topic")
    if not topic:
        raise SystemExit("no topic: pass --topic or run `ivideo new` first")
    model = a.model or os.environ.get("IVIDEO_DIALOGUE_MODEL") or dialogue.DEFAULT_MODEL
    data = dialogue.write_dialogue(
        proj,
        topic=topic,
        audience=a.audience or brief.get("audience", "curious beginners"),
        minutes=a.minutes or brief.get("minutes", 3.0),
        speakers=a.speakers or brief.get("speakers", 2),
        model=model,
        fallbacks=a.fallback if a.fallback else None,
        source_paths=(a.source or brief.get("sources") or []),
        notes=a.notes or brief.get("notes", ""),
        temperature=a.temperature,
        reasoning_effort=a.reasoning_effort or None,
        max_tokens=a.max_tokens,
        seed=a.seed,
    )
    return {
        "script": str(proj.script),
        "title": data["title"],
        "model": data["meta"]["dialogue_model"],
        "scenes": [
            {"id": s["id"], "title": s["title"], "lines": len(s["lines"])} for s in data["scenes"]
        ],
        "words": data["meta"]["words"],
        "estimated_minutes": data["meta"]["estimated_minutes"],
    }


def cmd_voice(a) -> dict:
    from .dialogue import load_script
    from .tts import synthesize

    proj = Project.open(a.project)
    script = load_script(proj)
    overrides = dict(v.split("=", 1) for v in (a.voice or []))
    timing = synthesize(
        proj,
        script,
        engine=a.engine,
        speed=a.speed,
        voice_overrides=overrides,
        only=a.only,
        model=a.tts_model,
    )
    st = proj.load_status()
    for sc in timing["scenes"]:
        st.setdefault("scenes", {}).setdefault(sc["id"], {"state": "todo", "note": ""})
    write_json(proj.status_file, st)
    return {
        "timing": str(proj.timing),
        "engine": timing["engine"],
        "tts_model": timing["tts_model"],
        "voices": timing["voices"],
        "total_duration": timing["total_duration"],
        "scenes": [
            {"id": s["id"], "duration": s["duration"], "lines": len(s["lines"])}
            for s in timing["scenes"]
        ],
    }


def cmd_brief(a) -> dict:
    proj = Project.open(a.project)
    timing = read_json(proj.timing)
    outline = [
        {"id": s["id"], "title": s["title"], "learning_goal": s["learning_goal"]}
        for s in timing["scenes"]
    ]
    out = {
        "project": str(proj.root),
        "video_title": timing["title"],
        "speakers": timing["speakers"],
        "outline": outline,
        "status": proj.load_status().get("scenes", {}),
    }
    if a.scene:
        sc = next((s for s in timing["scenes"] if s["id"] == a.scene), None)
        if sc is None:
            raise SystemExit(f"no scene {a.scene}")
        out["scene"] = sc
        out["scene_file"] = str(proj.scene_file(a.scene))
    else:
        out["scenes"] = timing["scenes"]
    return out


def cmd_render(a) -> dict:
    from .render import render_scene

    proj = Project.open(a.project)
    res = render_scene(proj, a.scene, quality=a.quality, frames=not a.no_frames)
    if res.get("ok") and a.quality == "draft":
        st = proj.load_status().get("scenes", {}).get(a.scene, {})
        if st.get("state") != "approved":
            proj.set_scene_state(a.scene, "rendered", "clean" if res.get("clean") else "has issues")
    return res


def cmd_render_all(a) -> dict:
    from .render import render_scene

    proj = Project.open(a.project)
    timing = read_json(proj.timing)
    results = [
        render_scene(proj, s["id"], quality=a.quality, frames=False) for s in timing["scenes"]
    ]
    return {
        "ok": all(r.get("ok") for r in results),
        "results": [
            {
                k: r.get(k)
                for k in (
                    "scene",
                    "ok",
                    "error",
                    "log_tail",
                    "video_duration",
                    "duration_error",
                    "clean",
                )
            }
            for r in results
        ],
    }


def cmd_frames(a) -> dict:
    from .render import extract_frames

    return extract_frames(Project.open(a.project), a.scene, a.quality)


def cmd_still(a) -> dict:
    from .render import render_still

    return render_still(Project.open(a.project), a.file, a.cls)


def cmd_mark(a) -> dict:
    proj = Project.open(a.project)
    return proj.set_scene_state(a.scene, a.state, a.note or "")


def cmd_assemble(a) -> dict:
    from .assemble import assemble

    return assemble(Project.open(a.project), quality=a.quality, burn_subtitles=a.burn_subtitles)


def cmd_status(a) -> dict:
    proj = Project.open(a.project)
    out = {
        "project": str(proj.root),
        "brief": proj.brief.exists(),
        "script": proj.script.exists(),
        "timing": proj.timing.exists(),
        "final": (proj.out_dir / "final.mp4").exists(),
        "scenes": {},
    }
    if proj.timing.exists():
        st = proj.load_status().get("scenes", {})
        for sc in read_json(proj.timing)["scenes"]:
            sid = sc["id"]
            out["scenes"][sid] = {
                "state": st.get(sid, {}).get("state", "todo"),
                "note": st.get(sid, {}).get("note", ""),
                "file": proj.scene_file(sid).exists(),
                "draft": proj.render_file(sid, "draft").exists(),
                "final": proj.render_file(sid, "final").exists(),
            }
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ivideo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)

    n = sub.add_parser("new")
    n.add_argument("project", nargs="?")
    n.add_argument("--topic", required=True)
    n.add_argument("--audience", default="curious beginners")
    n.add_argument("--minutes", type=float, default=3.0)
    n.add_argument("--speakers", type=int, default=2)
    n.add_argument("--notes", default="")
    n.add_argument("--source", action="append")
    n.set_defaults(fn=cmd_new)

    d = sub.add_parser("dialogue")
    d.add_argument("project")
    d.add_argument("--topic")
    d.add_argument("--audience")
    d.add_argument("--minutes", type=float)
    d.add_argument("--speakers", type=int)
    d.add_argument("--model")
    d.add_argument("--fallback", action="append")
    d.add_argument("--source", action="append")
    d.add_argument("--notes")
    d.add_argument("--temperature", type=float, help="sent only if the model supports it")
    d.add_argument(
        "--reasoning-effort", default="medium", help="low|medium|high; sent only if supported"
    )
    d.add_argument("--max-tokens", type=int, default=32000)
    d.add_argument("--seed", type=int)
    d.set_defaults(fn=cmd_dialogue)

    v = sub.add_parser("voice")
    v.add_argument("project")
    v.add_argument("--engine", default="openrouter", choices=["openrouter", "kokoro", "tone"])
    v.add_argument(
        "--tts-model", help="OpenRouter speech model (default heygen/heygen-voice-1-turbo:free)"
    )
    v.add_argument("--speed", type=float, default=1.0)
    v.add_argument("--voice", action="append", help="speaker_id=voice, e.g. host=nadine")
    v.add_argument("--only", action="append", help="re-synthesize only these scene ids")
    v.set_defaults(fn=cmd_voice)

    sr = sub.add_parser("script-rules", help="rules + JSON schema for writing script.json")
    sr.add_argument("project")
    sr.set_defaults(fn=cmd_script_rules)

    cs = sub.add_parser("check-script", help="validate script.json and assign scene ids")
    cs.add_argument("project")
    cs.add_argument("--writer", default="claude-opus-5-5 (Claude plan)")
    cs.set_defaults(fn=cmd_check_script)

    b = sub.add_parser("brief")
    b.add_argument("project")
    b.add_argument("scene", nargs="?")
    b.set_defaults(fn=cmd_brief)

    r = sub.add_parser("render")
    r.add_argument("project")
    r.add_argument("scene")
    r.add_argument("--quality", default="draft", choices=["draft", "final"])
    r.add_argument("--no-frames", action="store_true")
    r.set_defaults(fn=cmd_render)

    ra = sub.add_parser("render-all")
    ra.add_argument("project")
    ra.add_argument("--quality", default="final", choices=["draft", "final"])
    ra.set_defaults(fn=cmd_render_all)

    f = sub.add_parser("frames")
    f.add_argument("project")
    f.add_argument("scene")
    f.add_argument("--quality", default="draft", choices=["draft", "final"])
    f.set_defaults(fn=cmd_frames)

    sl = sub.add_parser("still", help="render the last frame of a plain Scene (e.g. the gallery)")
    sl.add_argument("project")
    sl.add_argument("file", help="path, or a file name inside the project's scenes/ folder")
    sl.add_argument("--class", dest="cls", default="Gallery")
    sl.set_defaults(fn=cmd_still)

    m = sub.add_parser("mark")
    m.add_argument("project")
    m.add_argument("scene")
    m.add_argument("state", choices=STATES)
    m.add_argument("--note")
    m.set_defaults(fn=cmd_mark)

    a = sub.add_parser("assemble")
    a.add_argument("project")
    a.add_argument("--quality", default="final", choices=["draft", "final"])
    a.add_argument("--burn-subtitles", action="store_true")
    a.set_defaults(fn=cmd_assemble)

    s = sub.add_parser("status")
    s.add_argument("project")
    s.set_defaults(fn=cmd_status)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    _print(args.fn(args))


if __name__ == "__main__":
    main()
