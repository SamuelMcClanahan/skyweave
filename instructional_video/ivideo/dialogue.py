"""Write the spoken dialogue with an OpenRouter model. Output: script.json."""

from __future__ import annotations

import json
from pathlib import Path

from . import openrouter
from .project import Project, write_json
from .schema import SCRIPT_SCHEMA, assign_scene_ids, total_words, validate_script

# Default model and fallbacks (research recommendation, RESEARCH.md; ids checked on
# openrouter.ai/api/v1/models 2026-11-03). Override with --model / --fallback or
# IVIDEO_DIALOGUE_MODEL. Prices in USD per million tokens, input / output:
#   anthropic/claude-opus-5.5   4 / 20
#   openai/gpt-6.1-sol          2 / 10
#   x-ai/grok-4.7               2 / 6
DEFAULT_MODEL = "anthropic/claude-opus-5.5"
DEFAULT_FALLBACKS = ["openai/gpt-6.1-sol", "x-ai/grok-4.7"]

WORDS_PER_MINUTE = 155  # Kokoro at speed 1.0 measures about 150-170 wpm.
MAX_SOURCE_CHARS = 80_000

SYSTEM_PROMPT = """You write scripts for short animated instructional videos.
The video is 2D motion graphics: text, shapes, arrows, diagrams, graphs, equations.
There are no people, faces, photos, or video footage on screen.
A separate animator draws each line's "visual" field. A text-to-speech engine reads each line's "text" field aloud.

Rules for "text" (spoken words):
- Plain spoken English. No markdown, no emoji, no brackets, no stage directions, no speaker names.
- At most 30 words per line. One idea per line. Short sentences.
- Write symbols and numbers as they are spoken: "x squared", "three point two meters", "about forty percent".
- Each speaker sounds like a person, not like a textbook. Contractions are fine.
- Dialogue mode: the teacher explains; the learner asks the questions a real beginner would ask, restates ideas in their own words, and makes one common mistake per video that the teacher corrects. Do not let the learner just say "Wow" or "Great".
- Narrator mode (one speaker): direct, warm, second person.

Rules for "visual" (what is on screen while the line plays):
- Concrete and drawable in 2D in a few seconds: name the objects, labels, arrows, colors, and motion.
- Build on what is already on screen. Say what appears, what moves, what is removed.
- Keep at most about 6 on-screen elements and 2 short text labels per beat. No paragraphs on screen.
- Every line must have a visual change or emphasis that matches the words.

Structure:
- Scene 1 hooks the viewer with a question or a surprising fact and says what they will learn.
- Middle scenes teach one concept each, in order, with a concrete example.
- The last scene recaps the key points in 2 or 3 lines.
- Each scene has 3 to 7 lines and lasts about 20 to 60 seconds.
- Be accurate. If source material is given, use only facts from it, and keep its terms.
"""


def build_messages(
    *,
    topic: str,
    audience: str,
    minutes: float,
    speakers: int,
    sources: list[tuple[str, str]],
    notes: str = "",
) -> list[dict]:
    words = int(minutes * WORDS_PER_MINUTE)
    mode = (
        "Dialogue mode with exactly 2 speakers: one with role 'teacher' (id 'host') "
        "and one with role 'learner' (id 'learner')."
        if speakers == 2
        else "Narrator mode with exactly 1 speaker: role 'narrator' (id 'narrator')."
        if speakers == 1
        else f"Dialogue mode with exactly {speakers} speakers; the first is the teacher."
    )
    user = [
        f"Topic: {topic}",
        f"Audience: {audience}",
        f"Target length: about {minutes:g} minutes, which is about {words} spoken words in total.",
        mode,
        "Return only the JSON object that matches the schema.",
    ]
    if notes:
        user.append(f"Extra instructions from the producer: {notes}")
    if sources:
        budget = MAX_SOURCE_CHARS
        user.append("Source material (ground every fact in this):")
        for name, text in sources:
            if budget <= 0:
                break
            chunk = text[:budget]
            budget -= len(chunk)
            user.append(f"<source name={json.dumps(name)}>\n{chunk}\n</source>")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user)},
    ]


def write_dialogue(
    project: Project,
    *,
    topic: str,
    audience: str = "curious beginners",
    minutes: float = 3.0,
    speakers: int = 2,
    model: str = DEFAULT_MODEL,
    fallbacks: list[str] | None = None,
    source_paths: list[str] | None = None,
    notes: str = "",
    temperature: float | None = None,
    seed: int | None = None,
    reasoning_effort: str | None = "medium",
    max_tokens: int = 32000,
    max_repairs: int = 2,
) -> dict:
    """Ask OpenRouter for the script, validate it, send problems back (max_repairs times).

    temperature and seed are sent only if set AND the model supports them.
    """
    project.ensure()
    sources = [(Path(p).name, Path(p).read_text(errors="replace")) for p in (source_paths or [])]
    messages = build_messages(
        topic=topic,
        audience=audience,
        minutes=minutes,
        speakers=speakers,
        sources=sources,
        notes=notes,
    )
    fallbacks = DEFAULT_FALLBACKS if fallbacks is None else fallbacks
    models = [model, *[m for m in fallbacks if m != model]]
    log: list[dict] = []
    data = None
    problems: list[str] = []
    for attempt in range(max_repairs + 1):
        failed: list[dict] = []
        try:
            comp = openrouter.chat(
                messages,
                models=models,
                schema=SCRIPT_SCHEMA,
                schema_name="instructional_video_script",
                max_tokens=max_tokens,
                temperature=temperature,
                seed=seed,
                reasoning_effort=reasoning_effort,
                attempts_log=failed,
            )
        except openrouter.TruncatedError as e:
            log.append({"attempt": attempt, "failed_models": failed, "error": str(e)})
            write_json(project.openrouter_log, log)
            raise SystemExit(str(e)) from None
        used = comp.request.get("model", model)
        entry = {
            "attempt": attempt,
            "failed_models": failed,
            "requested_model": used,
            "model": comp.model,
            "response": comp.raw,
            "finish_reason": comp.finish_reason,
            "usage": comp.usage,
            "request": comp.request,
            "content": comp.content,
        }
        log.append(entry)
        try:
            data = openrouter.parse_json_content(comp.content)
            problems = validate_script(data, speakers=speakers)
        except json.JSONDecodeError as e:
            data, problems = None, [f"output is not valid JSON: {e}"]
        entry["problems"] = problems
        write_json(project.openrouter_log, log)
        if not problems:
            break
        # Repairs go to the model that wrote the draft first.
        models = [used, *[m for m in models if m != used]]
        messages = [
            *messages,
            {"role": "assistant", "content": comp.content},
            {
                "role": "user",
                "content": "Fix these problems and return the complete corrected JSON only:\n- "
                + "\n- ".join(problems),
            },
        ]
    if problems or data is None:
        raise SystemExit(
            "OpenRouter script is still invalid after repairs:\n- " + "\n- ".join(problems)
        )
    data = assign_scene_ids(data)
    cost = sum(float((e.get("usage") or {}).get("cost") or 0) for e in log)
    data["meta"] = {
        "topic": topic,
        "audience": audience,
        "minutes": minutes,
        "dialogue_model": log[-1]["model"],
        "requested_model": model,
        "openrouter_cost_usd": round(cost, 4),
        "words": total_words(data),
        "estimated_minutes": round(total_words(data) / WORDS_PER_MINUTE, 2),
    }
    write_json(project.script, data)
    return data


def load_script(project: Project) -> dict:
    data = json.loads(project.script.read_text())
    problems = validate_script(data)
    if problems:
        raise SystemExit(f"{project.script} is invalid:\n- " + "\n- ".join(problems))
    if any("id" not in sc for sc in data["scenes"]):
        data = assign_scene_ids(data)
    return data
