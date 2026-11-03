"""Speak every line with Kokoro-82M (ONNX, CPU) and write timing.json.

Audio comes first. The animation is then timed to the measured audio, so
sync never drifts: each scene's video must hit line start times from timing.json.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np
import soundfile as sf

from .project import MODELS_DIR, Project, write_json

SAMPLE_RATE = 24_000
LEAD_IN = 0.6  # silence before the first line of a scene
GAP_SAME = 0.35  # silence between two lines by the same speaker
GAP_SWITCH = 0.55  # silence when the speaker changes
TAIL = 1.0  # silence after the last line of a scene

# Voice per speaker role. Kokoro v1.0 voice ids (American English).
DEFAULT_VOICES = {"host": "af_heart", "learner": "am_michael", "narrator": "af_heart"}
SPARE_VOICES = ["am_puck", "af_bella", "bm_george", "bf_emma"]

_SPOKEN = [
    (re.compile(r"\s*[—–]\s*"), ", "),  # long dashes read as a pause
    (re.compile(r"\s+"), " "),
]


def clean_for_speech(text: str) -> str:
    for rx, rep in _SPOKEN:
        text = rx.sub(rep, text)
    return text.strip()


class Synth:
    """Wraps a TTS engine. engine='kokoro' (default) or 'tone' (tests and dry runs:
    a quiet 220 Hz beep, 0.4 s per word, no model needed)."""

    def __init__(self, engine: str = "kokoro", speed: float = 1.0):
        self.engine = engine
        self.speed = speed
        self._k = None
        if engine == "kokoro":
            from kokoro_onnx import Kokoro

            model = Path(os.environ.get("IVIDEO_KOKORO_MODEL", MODELS_DIR / "kokoro-v1.0.onnx"))
            voices = Path(os.environ.get("IVIDEO_KOKORO_VOICES", MODELS_DIR / "voices-v1.0.bin"))
            if not model.exists() or not voices.exists():
                raise SystemExit(
                    f"Kokoro model files missing in {model.parent}. Run ./setup.sh first."
                )
            self._k = Kokoro(str(model), str(voices))
        elif engine != "tone":
            raise ValueError(f"unknown TTS engine {engine!r}")

    def voices(self) -> list[str]:
        return sorted(self._k.get_voices()) if self._k else []

    def say(self, text: str, voice: str) -> np.ndarray:
        if self._k is None:  # tone engine: deterministic, 0.4 s per word
            n = int(SAMPLE_RATE * 0.4 * max(1, len(text.split())))
            return (0.1 * np.sin(2 * np.pi * 220 * np.arange(n) / SAMPLE_RATE)).astype(np.float32)
        samples, sr = self._k.create(
            clean_for_speech(text), voice=voice, speed=self.speed, lang="en-us"
        )
        if sr != SAMPLE_RATE:
            raise RuntimeError(f"unexpected sample rate {sr}")
        return np.asarray(samples, dtype=np.float32)


def assign_voices(speakers: list[dict], overrides: dict[str, str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    spare = iter(SPARE_VOICES)
    for s in speakers:
        sid = s["id"]
        v = (overrides or {}).get(sid) or DEFAULT_VOICES.get(sid)
        if v is None or v in out.values():
            role = s.get("role", "").lower()
            v = DEFAULT_VOICES.get(role) if "narrat" in role else None
            if v is None or v in out.values():
                v = next(spare)
        out[sid] = v
    return out


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * SAMPLE_RATE)), dtype=np.float32)


def _trim(x: np.ndarray, thresh: float = 1e-3, pad: float = 0.03) -> np.ndarray:
    """Cut leading/trailing near-silence so gaps are set by us, not by the engine."""
    idx = np.flatnonzero(np.abs(x) > thresh)
    if idx.size == 0:
        return x
    p = int(pad * SAMPLE_RATE)
    return x[max(0, idx[0] - p) : min(len(x), idx[-1] + p + 1)]


def synthesize(
    project: Project,
    script: dict,
    *,
    engine: str = "kokoro",
    speed: float = 1.0,
    voice_overrides: dict[str, str] | None = None,
    only: list[str] | None = None,
) -> dict:
    """Write audio/<sid>/<n>.wav, audio/<sid>.wav and timing.json. Returns the timing dict."""
    project.ensure()
    synth = Synth(engine, speed)
    voices = assign_voices(script["speakers"], voice_overrides)
    scenes_out = []
    for sc in script["scenes"]:
        sid = sc["id"]
        line_dir = project.audio_dir / sid
        line_dir.mkdir(parents=True, exist_ok=True)
        cursor = LEAD_IN
        chunks = [_silence(LEAD_IN)]
        lines_out = []
        prev = None
        for i, ln in enumerate(sc["lines"]):
            if prev is not None:
                gap = GAP_SAME if ln["speaker"] == prev else GAP_SWITCH
                chunks.append(_silence(gap))
                cursor += gap
            wav = line_dir / f"{i:02d}.wav"
            if only is None or sid in only or not wav.exists():
                audio = _trim(synth.say(ln["text"], voices[ln["speaker"]]))
                sf.write(wav, audio, SAMPLE_RATE, subtype="PCM_16")
            else:
                audio, _ = sf.read(wav, dtype="float32")
            dur = len(audio) / SAMPLE_RATE
            chunks.append(audio)
            lines_out.append(
                {
                    "index": i,
                    "speaker": ln["speaker"],
                    "voice": voices[ln["speaker"]],
                    "text": ln["text"],
                    "visual": ln["visual"],
                    "start": round(cursor, 3),
                    "end": round(cursor + dur, 3),
                    "duration": round(dur, 3),
                    "audio": str(wav.relative_to(project.root)),
                }
            )
            cursor += dur
            prev = ln["speaker"]
        chunks.append(_silence(TAIL))
        cursor += TAIL
        scene_audio = np.concatenate(chunks)
        scene_wav = project.audio_dir / f"{sid}.wav"
        sf.write(scene_wav, scene_audio, SAMPLE_RATE, subtype="PCM_16")
        scenes_out.append(
            {
                "id": sid,
                "title": sc["title"],
                "learning_goal": sc["learning_goal"],
                "visual_plan": sc["visual_plan"],
                "duration": round(len(scene_audio) / SAMPLE_RATE, 3),
                "audio": str(scene_wav.relative_to(project.root)),
                "lines": lines_out,
            }
        )
    timing = {
        "title": script["title"],
        "sample_rate": SAMPLE_RATE,
        "engine": engine,
        "voices": voices,
        "speakers": script["speakers"],
        "total_duration": round(sum(s["duration"] for s in scenes_out), 3),
        "scenes": scenes_out,
    }
    write_json(project.timing, timing)
    return timing
