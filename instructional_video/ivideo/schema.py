"""The dialogue script format. OpenRouter must return JSON in this shape.

The JSON Schema is "strict" compatible (every property required, no extra
properties), so it works with OpenRouter structured outputs. validate_script()
does the same checks locally plus the content rules, because not every model
honors the schema.
"""

from __future__ import annotations

import re

LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker": {"type": "string", "description": "speaker id from the speakers list"},
        "text": {
            "type": "string",
            "description": "exact words spoken aloud; plain spoken English, no markdown, no stage directions",
        },
        "visual": {
            "type": "string",
            "description": "the on-screen beat while this line plays: concrete 2D shapes, text, arrows, graphs",
        },
    },
    "required": ["speaker", "text", "visual"],
    "additionalProperties": False,
}

SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "learning_goal": {"type": "string"},
        "visual_plan": {
            "type": "string",
            "description": "overall picture for the scene: layout, main objects, how they change",
        },
        "lines": {"type": "array", "items": LINE_SCHEMA},
    },
    "required": ["title", "learning_goal", "visual_plan", "lines"],
    "additionalProperties": False,
}

SPEAKER_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "short lowercase id, for example host or learner"},
        "name": {"type": "string"},
        "role": {"type": "string", "description": "teacher, learner, narrator"},
    },
    "required": ["id", "name", "role"],
    "additionalProperties": False,
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "speakers": {"type": "array", "items": SPEAKER_SCHEMA},
        "scenes": {"type": "array", "items": SCENE_SCHEMA},
    },
    "required": ["title", "summary", "speakers", "scenes"],
    "additionalProperties": False,
}

MAX_WORDS_PER_LINE = 40
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_MARKUP_RE = re.compile(r"(\*\*|__|`|^#|\[[^\]]*\]\(|<[^>]+>)")
_STAGE_RE = re.compile(r"^\s*[\(\[][^\)\]]*[\)\]]\s*")


class ScriptError(ValueError):
    """The script JSON breaks the format. The message lists every problem."""


def word_count(text: str) -> int:
    return len(text.split())


def validate_script(data: dict, *, speakers: int | None = None) -> list[str]:
    """Return a list of problems. An empty list means the script is valid."""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["top level is not a JSON object"]
    for key in ("title", "summary"):
        if not isinstance(data.get(key), str) or not data.get(key, "").strip():
            errs.append(f"'{key}' must be a non-empty string")
    sp = data.get("speakers")
    ids: set[str] = set()
    if not isinstance(sp, list) or not sp:
        errs.append("'speakers' must be a non-empty list")
    else:
        for i, s in enumerate(sp):
            if not isinstance(s, dict):
                errs.append(f"speakers[{i}] is not an object")
                continue
            sid = s.get("id", "")
            if not isinstance(sid, str) or not _ID_RE.match(sid):
                errs.append(f"speakers[{i}].id {sid!r} must match {_ID_RE.pattern}")
            elif sid in ids:
                errs.append(f"speakers[{i}].id {sid!r} is duplicated")
            ids.add(sid)
            for key in ("name", "role"):
                if not isinstance(s.get(key), str) or not s.get(key, "").strip():
                    errs.append(f"speakers[{i}].{key} must be a non-empty string")
        if speakers is not None and len(sp) != speakers:
            errs.append(f"expected {speakers} speakers, got {len(sp)}")
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errs.append("'scenes' must be a non-empty list")
        return errs
    used: set[str] = set()
    for si, sc in enumerate(scenes):
        where = f"scenes[{si}]"
        if not isinstance(sc, dict):
            errs.append(f"{where} is not an object")
            continue
        for key in ("title", "learning_goal", "visual_plan"):
            if not isinstance(sc.get(key), str) or not sc.get(key, "").strip():
                errs.append(f"{where}.{key} must be a non-empty string")
        lines = sc.get("lines")
        if not isinstance(lines, list) or not lines:
            errs.append(f"{where}.lines must be a non-empty list")
            continue
        for li, ln in enumerate(lines):
            lw = f"{where}.lines[{li}]"
            if not isinstance(ln, dict):
                errs.append(f"{lw} is not an object")
                continue
            if ids and ln.get("speaker") not in ids:
                errs.append(f"{lw}.speaker {ln.get('speaker')!r} is not a declared speaker id")
            else:
                used.add(ln.get("speaker"))
            text = ln.get("text")
            if not isinstance(text, str) or not text.strip():
                errs.append(f"{lw}.text must be a non-empty string")
            else:
                n = word_count(text)
                if n > MAX_WORDS_PER_LINE:
                    errs.append(f"{lw}.text has {n} words; max is {MAX_WORDS_PER_LINE}")
                if _MARKUP_RE.search(text):
                    errs.append(f"{lw}.text contains markup; spoken text must be plain")
                if _STAGE_RE.match(text):
                    errs.append(f"{lw}.text starts with a stage direction in brackets")
            if not isinstance(ln.get("visual"), str) or not ln.get("visual", "").strip():
                errs.append(f"{lw}.visual must be a non-empty string")
    if ids and used and ids - used:
        errs.append(f"speakers never used: {sorted(ids - used)}")
    return errs


def assign_scene_ids(data: dict) -> dict:
    """Give scenes stable ids s01, s02, ... (the model does not choose ids)."""
    for i, sc in enumerate(data["scenes"], start=1):
        sc["id"] = f"s{i:02d}"
    return data


def total_words(data: dict) -> int:
    return sum(word_count(ln["text"]) for sc in data["scenes"] for ln in sc["lines"])
