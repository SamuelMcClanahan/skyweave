"""Speak every line and write timing.json.

Engines: "openrouter" (default; OpenRouter /audio/speech, model
heygen/heygen-voice-1-turbo:free), "kokoro" (local Kokoro-82M, no key),
"tone" (tests: a quiet beep).

Audio comes first. The animation is then timed to the measured audio, so
sync never drifts: each scene's video must hit line start times from timing.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from .project import MODELS_DIR, Project, write_json

SAMPLE_RATE = 24_000
LEAD_IN = 0.6  # silence before the first line of a scene
GAP_SAME = 0.35  # silence between two lines by the same speaker
GAP_SWITCH = 0.55  # silence when the speaker changes
TAIL = 1.0  # silence after the last line of a scene

DEFAULT_ENGINE = "openrouter"

# Voice per speaker id, per engine.
# HeyGen default voices: nadine, tabitha, bram, gareth, noelle, meredith, sabrina,
# lucian, weston, vincent. Kokoro v1.0 ids are American English voices.
ENGINE_VOICES = {
    "openrouter": (
        {"host": "nadine", "learner": "bram", "narrator": "nadine"},
        ["gareth", "tabitha", "lucian", "noelle", "weston", "meredith"],
    ),
    "kokoro": (
        {"host": "af_heart", "learner": "am_michael", "narrator": "af_heart"},
        ["am_puck", "af_bella", "bm_george", "bf_emma"],
    ),
    "tone": ({"host": "a", "learner": "b", "narrator": "a"}, ["c", "d", "e", "f"]),
}
DEFAULT_VOICES, SPARE_VOICES = ENGINE_VOICES["kokoro"]

_SPOKEN = [
    (re.compile(r"\s*[—–]\s*"), ", "),  # long dashes read as a pause
    (re.compile(r"\s+"), " "),
]


def clean_for_speech(text: str) -> str:
    for rx, rep in _SPOKEN:
        text = rx.sub(rep, text)
    return text.strip()


def decode_audio(data: bytes, ctype: str = "") -> np.ndarray:
    """Any audio bytes (mp3, wav, ...) -> mono float32 at SAMPLE_RATE, via ffmpeg."""
    fmt = ["-f", "s16le", "-ar", "24000", "-ac", "1"] if "pcm" in ctype else []
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            *fmt,
            "-i",
            "pipe:0",
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "pipe:1",
        ],
        input=data,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"could not decode {ctype or 'audio'}: {proc.stderr.decode()[-500:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


class Synth:
    """Wraps a TTS engine: 'openrouter' (default), 'kokoro', or 'tone' (tests and
    dry runs: a quiet 220 Hz beep, 0.4 s per word, no model or key needed)."""

    def __init__(self, engine: str = DEFAULT_ENGINE, speed: float = 1.0, model: str | None = None):
        self.engine = engine
        self.speed = speed
        self._k = None
        self.model = None
        if engine == "openrouter":
            from . import openrouter

            openrouter.api_key()  # fail early with a clear message
            self.model = (
                model or os.environ.get("IVIDEO_TTS_MODEL") or openrouter.DEFAULT_SPEECH_MODEL
            )
        elif engine == "kokoro":
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

    def signature(self, text: str, voice: str) -> str:
        """Hash of everything that changes the audio of a line (cache key)."""
        key = json.dumps([self.engine, self.model, voice, self.speed, clean_for_speech(text)])
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def voices(self) -> list[str]:
        return sorted(self._k.get_voices()) if self._k else []

    def say(self, text: str, voice: str) -> np.ndarray:
        if self.engine == "openrouter":
            from . import openrouter

            data, ctype = openrouter.speech(
                clean_for_speech(text), model=self.model, voice=voice, speed=self.speed
            )
            return decode_audio(data, ctype)
        if self._k is None:  # tone engine: deterministic, 0.4 s per word
            n = int(SAMPLE_RATE * 0.4 * max(1, len(text.split())))
            return (0.1 * np.sin(2 * np.pi * 220 * np.arange(n) / SAMPLE_RATE)).astype(np.float32)
        samples, sr = self._k.create(
            clean_for_speech(text), voice=voice, speed=self.speed, lang="en-us"
        )
        if sr != SAMPLE_RATE:
            raise RuntimeError(f"unexpected sample rate {sr}")
        return np.asarray(samples, dtype=np.float32)


def assign_voices(
    speakers: list[dict], overrides: dict[str, str] | None = None, engine: str = "kokoro"
) -> dict[str, str]:
    defaults, spares = ENGINE_VOICES.get(engine, ENGINE_VOICES["kokoro"])
    out: dict[str, str] = {}
    spare = iter(spares)
    for s in speakers:
        sid = s["id"]
        v = (overrides or {}).get(sid) or defaults.get(sid)
        if v is None or v in out.values():
            role = s.get("role", "").lower()
            v = defaults.get("narrator") if "narrat" in role else None
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
    engine: str = DEFAULT_ENGINE,
    speed: float = 1.0,
    voice_overrides: dict[str, str] | None = None,
    only: list[str] | None = None,
    model: str | None = None,
) -> dict:
    """Write audio/<sid>/<n>.wav, audio/<sid>.wav and timing.json. Returns the timing dict.

    A line is re-synthesized only if its text, voice, engine, model or speed changed
    (or its scene is listed in `only`)."""
    project.ensure()
    synth = Synth(engine, speed, model)
    voices = assign_voices(script["speakers"], voice_overrides, engine)
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
            sig_file = wav.with_suffix(".sig")
            sig = synth.signature(ln["text"], voices[ln["speaker"]])
            fresh = wav.exists() and sig_file.exists() and sig_file.read_text() == sig
            if (only is not None and sid in only) or not fresh:
                audio = _trim(synth.say(ln["text"], voices[ln["speaker"]]))
                sf.write(wav, audio, SAMPLE_RATE, subtype="PCM_16")
                sig_file.write_text(sig)
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
        "tts_model": synth.model,
        "voices": voices,
        "speakers": script["speakers"],
        "total_duration": round(sum(s["duration"] for s in scenes_out), 3),
        "scenes": scenes_out,
    }
    write_json(project.timing, timing)
    return timing
