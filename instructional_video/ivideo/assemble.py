"""Join final scene renders with their narration into one MP4 (+ SRT subtitles).

Per scene: hold the last video frame if the audio is longer (tpad clone), pad
the audio with silence if the video is longer (apad), encode H.264 + AAC.
Then concat all scenes (concat demuxer, same codec settings), normalize
loudness (EBU R128, -16 LUFS), add soft subtitles, and set faststart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .project import Project, read_json, write_json
from .render import contact_sheet, grab_frame, probe_duration

FPS = 30
AR = 48_000


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg failed: {' '.join(cmd)}\n{proc.stderr[-3000:]}")


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
    segments: list[Path] = []
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
        dur = max(vd, ad)
        seg = seg_dir / f"{sid}.mp4"
        vf = [f"fps={FPS}", "format=yuv420p"]
        if ad > vd + 1e-3:
            vf.insert(0, f"tpad=stop_mode=clone:stop_duration={ad - vd + 0.1:.3f}")
        _run([
            "ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(audio),
            "-filter_complex", f"[0:v]{','.join(vf)}[v];[1:a]aresample={AR},apad[a]",
            "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", str(AR), "-ac", "2",
            str(seg),
        ])  # fmt: skip
        actual = probe_duration(seg)
        offsets[sid] = cursor
        cursor += actual
        segments.append(seg)
        per_scene.append(
            {
                "scene": sid,
                "video": round(vd, 3),
                "audio": round(ad, 3),
                "segment": round(actual, 3),
            }
        )
    concat_list = seg_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{p.name}'\n" for p in segments))
    joined = seg_dir / "joined.mp4"
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
            str(concat_list),
            "-c",
            "copy",
            str(joined),
        ]
    )
    names = {s["id"]: s["name"] for s in timing.get("speakers", [])} if speaker_names else None
    srt = project.out_dir / "final.srt"
    srt.write_text(build_srt(timing, offsets, names))
    final = project.out_dir / "final.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(joined)]
    if burn_subtitles:
        style = "FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Outline=1,MarginV=24"
        cmd += [
            "-vf",
            f"subtitles={srt}:force_style='{style}'",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        ]
    else:
        cmd += [
            "-i",
            str(srt),
            "-map",
            "0:v",
            "-map",
            "0:a",
            "-map",
            "1:s",
            "-c:v",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=eng",
        ]
    cmd += [
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        str(AR),
        "-movflags",
        "+faststart",
        str(final),
    ]
    _run(cmd)
    # A preview sheet: the frame at the middle of every scene.
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
    result = {
        "final": str(final),
        "subtitles": str(srt),
        "contact_sheet": str(sheet),
        "duration": round(probe_duration(final), 3),
        "expected_duration": round(cursor, 3),
        "scenes": per_scene,
        "offsets": {k: round(v, 3) for k, v in offsets.items()},
        "streams": _streams(final),
    }
    write_json(project.out_dir / "assembly.json", result)
    return result


def _streams(path: Path) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,sample_rate",
         "-of", "compact=p=0:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [ln for ln in out.splitlines() if ln.strip()]
