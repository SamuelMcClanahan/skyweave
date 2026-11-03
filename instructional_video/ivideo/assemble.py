"""Join final scene renders with their narration into one MP4 (+ SRT subtitles).

Audio is encoded once (research finding: per-scene AAC segments joined with
`-c copy` drift 21-26 ms per cut). So:

1. Each scene gets a frame-exact length T = ceil(max(video, audio) * FPS) / FPS.
2. Video-only segment per scene: hold the last frame (tpad clone) up to T.
3. PCM WAV per scene: pad with silence and trim to exactly T.
4. Join the video segments (concat demuxer, stream copy) and the WAVs (PCM).
5. Two-pass EBU R128 loudness (-16 LUFS, -1.5 dBTP), one AAC encode, soft
   subtitles (or burned in), faststart.
6. Deterministic QA: duration, streams, loudness, long silences, subtitle count.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

from .project import Project, read_json, write_json
from .render import contact_sheet, grab_frame, probe_duration

FPS = 30
AR = 48_000
TARGET_LUFS = -16.0
TARGET_TP = -1.5
MAX_SILENCE = 1.7  # longest planned gap is tail 1.0 s + next lead-in 0.6 s
AAC_PAD = 0.05  # AAC encodes whole 1024-sample frames (21.3 ms at 48 kHz) plus priming


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg failed: {' '.join(cmd)}\n{proc.stderr[-3000:]}")
    return proc


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(timing: dict, offsets: dict[str, float], names: dict[str, str] | None = None) -> str:
    out, n = [], 1
    for sc in timing["scenes"]:
        off = offsets[sc["id"]]
        for ln in sc["lines"]:
            who = f"{names[ln['speaker']]}: " if names else ""
            out.append(
                f"{n}\n{_srt_time(off + ln['start'])} --> {_srt_time(off + ln['end'])}\n{who}{ln['text']}\n"
            )
            n += 1
    return "\n".join(out)


def frame_len(seconds: float, fps: int = FPS) -> tuple[int, float]:
    """Frames and exact seconds for a segment that covers `seconds`."""
    nf = math.ceil(round(seconds * fps, 6))
    return nf, nf / fps


def _loudness_pass1(wav: Path) -> dict:
    proc = _run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(wav),
        "-af", f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA=11:print_format=json", "-f", "null", "-",
    ])  # fmt: skip
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not m:
        raise SystemExit("loudnorm pass 1 printed no measurement")
    return json.loads(m.group(0))


def assemble(
    project: Project,
    quality: str = "final",
    burn_subtitles: bool = False,
    speaker_names: bool = True,
) -> dict:
    timing = read_json(project.timing)
    seg_dir = project.out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    offsets: dict[str, float] = {}
    vsegs: list[Path] = []
    asegs: list[Path] = []
    cursor = 0.0
    per_scene = []
    for sc in timing["scenes"]:
        sid = sc["id"]
        video = project.render_file(sid, quality)
        if not video.exists():
            raise SystemExit(
                f"missing render {video}. Run `ivideo render {project.root.name} {sid} --quality {quality}`."
            )
        audio = project.root / sc["audio"]
        vd, ad = probe_duration(video), probe_duration(audio)
        nf, seg_t = frame_len(max(vd, ad))
        vseg, aseg = seg_dir / f"{sid}.mp4", seg_dir / f"{sid}.wav"
        _run([
            "ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-vf", f"tpad=stop_mode=clone:stop=-1,fps={FPS},format=yuv420p",
            "-frames:v", str(nf), "-an",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-tune", "animation", "-g", str(2 * FPS),
            str(vseg),
        ])  # fmt: skip
        _run([
            "ffmpeg", "-v", "error", "-y", "-i", str(audio),
            "-af", f"aresample={AR},apad=whole_dur={seg_t:.6f},atrim=end={seg_t:.6f}",
            "-ac", "1", "-c:a", "pcm_s16le", str(aseg),
        ])  # fmt: skip
        offsets[sid] = cursor
        cursor += seg_t
        vsegs.append(vseg)
        asegs.append(aseg)
        per_scene.append(
            {
                "scene": sid,
                "video": round(vd, 3),
                "audio": round(ad, 3),
                "segment": round(seg_t, 4),
                "frames": nf,
            }
        )

    def concat(paths: list[Path], name: str) -> Path:
        lst = seg_dir / f"{name}.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in paths))
        out = seg_dir / name
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c",
                "copy",
                str(out),
            ]
        )
        return out

    video_all = concat(vsegs, "video_all.mp4")
    audio_all = concat(asegs, "audio_all.wav")

    names = {s["id"]: s["name"] for s in timing.get("speakers", [])} if speaker_names else None
    srt = project.out_dir / "final.srt"
    srt.write_text(build_srt(timing, offsets, names))

    m = _loudness_pass1(audio_all)
    if "inf" in str(m.get("input_i")):  # all silence: nothing to normalize
        norm = f"aresample={AR}"
    else:
        norm = (
            f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA=11:measured_I={m['input_i']}"
            f":measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
            f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,aresample={AR}"
        )
    final = project.out_dir / "final.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(video_all), "-i", str(audio_all)]
    if burn_subtitles:
        style = "FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Outline=1,MarginV=24"
        cmd += ["-map", "0:v", "-map", "1:a", "-vf", f"subtitles={srt}:force_style='{style}'"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "copy"]
        cmd += ["-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
    cmd += ["-af", norm, "-c:a", "aac", "-b:a", "160k", "-ar", str(AR), "-ac", "2"]
    # Cut at the video length: AAC pads its last frame, which would make the file longer.
    cmd += ["-t", f"{cursor:.6f}", "-movflags", "+faststart", str(final)]
    _run(cmd)

    # A preview sheet: one frame from the middle of every scene.
    previews = []
    for sc in timing["scenes"]:
        t = offsets[sc["id"]] + sc["duration"] * 0.6
        p = project.out_dir / "preview" / f"{sc['id']}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        grab_frame(final, t, p)
        previews.append((p, f"{sc['id']} @ {t:.1f}s  {sc['title']}"))
    sheet = contact_sheet(
        [p for p, _ in previews],
        [c for _, c in previews],
        project.out_dir / "contact_sheet.png",
        cols=min(3, len(previews)),
        width=480,
    )
    n_lines = sum(len(sc["lines"]) for sc in timing["scenes"])
    result = {
        "final": str(final),
        "subtitles": str(srt),
        "contact_sheet": str(sheet),
        "duration": round(probe_duration(final), 3),
        "expected_duration": round(cursor, 3),
        "scenes": per_scene,
        "offsets": {k: round(v, 4) for k, v in offsets.items()},
        "streams": _streams(final),
        "qa": qa(
            final,
            expected=cursor,
            frames=sum(p["frames"] for p in per_scene),
            n_lines=n_lines,
            srt=srt,
            burned=burn_subtitles,
        ),
    }
    write_json(project.out_dir / "assembly.json", result)
    return result


def _streams(path: Path) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,width,height,sample_rate",
         "-of", "compact=p=0:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [ln for ln in out.splitlines() if ln.strip()]


def qa(final: Path, *, expected: float, frames: int, n_lines: int, srt: Path, burned: bool) -> dict:
    """Deterministic checks on the finished file. `ok` is true only if all pass."""
    checks: dict = {}
    dur = probe_duration(final)
    vframes = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(final)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    checks["video_frames"] = int(vframes) if vframes.isdigit() else None
    checks["expected_frames"] = frames
    checks["duration_error_s"] = round(dur - expected, 4)
    # Video must be frame-exact. The file may run up to AAC_PAD longer (AAC frame padding).
    checks["duration_ok"] = checks["video_frames"] == frames and -1e-3 <= dur - expected <= AAC_PAD
    streams = " ".join(_streams(final))
    checks["streams_ok"] = (
        "h264" in streams
        and "yuv420p" in streams
        and "aac" in streams
        and (burned or "mov_text" in streams)
    )
    proc = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(final),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ]
    )
    tail = proc.stderr[proc.stderr.rfind("Summary:") :]
    i_m = re.search(r"I:\s+(-?[\d.]+) LUFS", tail)
    p_m = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", tail)
    lufs = float(i_m.group(1)) if i_m else None
    peak = float(p_m.group(1)) if p_m and p_m.group(1) != "-inf" else None
    checks["integrated_lufs"] = lufs
    checks["true_peak_dbfs"] = peak
    checks["loudness_ok"] = (
        lufs is not None
        and abs(lufs - TARGET_LUFS) <= 1.0
        and (peak is None or peak <= TARGET_TP + 0.5)
    )
    proc = _run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(final),
        "-af", f"silencedetect=n=-50dB:d={MAX_SILENCE}", "-f", "null", "-",
    ])  # fmt: skip
    long_sil = [float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", proc.stderr)]
    checks["long_silences_s"] = long_sil
    checks["silence_ok"] = not long_sil
    cues = srt.read_text().count("-->")
    checks["subtitle_cues"] = cues
    checks["subtitles_ok"] = cues == n_lines
    checks["ok"] = all(v for k, v in checks.items() if k.endswith("_ok"))
    return checks
