import soundfile as sf

from ivideo import tts


def test_timing_math(project):
    timing = __import__("json").loads(project.timing.read_text())
    s1 = timing["scenes"][0]
    lines = s1["lines"]
    # tone engine: 0.4 s per word; speakers alternate learner, host, learner
    assert lines[0]["start"] == tts.LEAD_IN
    assert abs(lines[1]["start"] - (lines[0]["end"] + tts.GAP_SWITCH)) < 1e-3
    assert abs(s1["duration"] - (lines[-1]["end"] + tts.TAIL)) < 1e-3
    audio, sr = sf.read(project.root / s1["audio"])
    assert sr == tts.SAMPLE_RATE
    assert abs(len(audio) / sr - s1["duration"]) < 1e-3


def test_voice_assignment():
    sp = [
        {"id": "host", "role": "teacher"},
        {"id": "learner", "role": "learner"},
        {"id": "guest", "role": "expert"},
    ]
    v = tts.assign_voices(sp)
    assert v["host"] == "af_heart" and v["learner"] == "am_michael"
    assert len(set(v.values())) == 3
    assert tts.assign_voices(sp, {"host": "bf_emma"})["host"] == "bf_emma"


def test_clean_for_speech():
    assert tts.clean_for_speech("a — b\n c") == "a, b c"
