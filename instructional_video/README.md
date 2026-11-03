# ivideo: animated instructional videos

This tool makes a short animated teaching video from a topic.

- **OpenRouter** writes the spoken dialogue (the script).
- **Kokoro-82M** (a free, local text-to-speech model) speaks each line.
- **Claude Opus agents** (your Claude account, through a Claude Code Workflow) write
  Manim animation code for each scene, render it, look at the frames, and fix problems.
- **ffmpeg** joins the scenes, the voice, and the subtitles into one MP4.

This folder is separate from SkyWeave. It does not touch `v1/` or `v2/`.

## 1. Set up (one time per machine)

```bash
cd instructional_video
./setup.sh            # system packages, Python env, Kokoro voice model (about 350 MB)
./setup.sh --latex    # optional: adds LaTeX so scenes can show formulas with MathTex
```

The setup script needs Ubuntu or Debian with `apt`, and `uv`.
It ends with `uv run ivideo doctor`, which prints what is ready.

Set your OpenRouter key as an environment variable:

```bash
export OPENROUTER_API_KEY=...      # from https://openrouter.ai/keys
```

In a Claude Code cloud session, add `OPENROUTER_API_KEY` in the environment settings.
Do not commit the key. The repository has a secret scanner.

## 2. Make a video with the Workflow (the normal way)

In Claude Code, in this repository, ask:

> Run the instructional-video workflow with topic "How a Kalman filter tracks a drone", 3 minutes.

The workflow file is `.claude/workflows/instructional-video.js`.
Claude Code loads it when a session starts.
If the name is not found, run it by path:
`Workflow({scriptPath: ".claude/workflows/instructional-video.js", args: {...}})`.

Workflow arguments (all optional except a topic for a new project):

| Argument | Meaning | Default |
| --- | --- | --- |
| `topic` | What the video teaches | none |
| `project` | Folder name under `projects/`. Use it again to resume. | slug of topic |
| `minutes` | Target length | 3 |
| `audience` | Who watches | curious beginners |
| `speakers` | 1 = narrator, 2 = teacher + learner dialogue | 2 |
| `sources` | Files with facts to use (paths) | none |
| `notes` | Extra instructions for the scriptwriter | none |
| `model` | OpenRouter model id for the dialogue | see `ivideo/dialogue.py` |
| `fallbacks` | More OpenRouter model ids, tried in order | see `ivideo/dialogue.py` |
| `script_file` | Use this script JSON. Skips OpenRouter. | none |
| `redo_dialogue` | Ask OpenRouter for a new script | false |
| `redo_scenes` | Scene ids to animate again, for example `["s02"]` | none |
| `voices` | Kokoro voice per speaker, for example `{"host": "bf_emma"}` | af_heart, am_michael |
| `speed` | Speech speed | 1.0 |
| `max_fix_rounds` | Review-and-repair rounds per scene | 2 |
| `max_renders` | Renders per animator agent | 6 |
| `burn_subtitles` | Draw subtitles into the picture | false (soft subtitles) |
| `latex` | Install LaTeX during setup | false |

What the workflow does:

1. **Prepare** (1 agent): checks the setup, gets the dialogue from OpenRouter,
   speaks every line, and measures the time of each line.
2. **Animate** (1 Claude Opus agent per scene, in parallel): writes `scenes/sNN.py`,
   renders a 480p draft, looks at one frame per line, and fixes problems.
3. **Review** (1 Claude Opus agent per scene, that did not write the code):
   looks at every frame and approves or rejects. A rejected scene goes to a
   repair agent, then to review again (at most `max_fix_rounds` times).
4. **Assemble** (1 agent): renders all scenes at 1080p 30 fps and joins them.
5. **Final check** (1 Claude Opus agent): checks length, audio level, streams,
   and that all scenes look like one video.

Output: `projects/<project>/out/final.mp4`, `final.srt`, `contact_sheet.png`.

Run it again with the same `project` to continue. Approved scenes stay as they are.

## 3. Commands (the workflow agents use these; you can too)

```bash
uv run ivideo doctor
uv run ivideo new <project> --topic "..." --minutes 3 --speakers 2 [--source notes.md]
uv run ivideo dialogue <project> [--model id] [--fallback id]   # calls OpenRouter
uv run ivideo voice <project> [--voice host=af_heart] [--speed 1.0]
uv run ivideo brief <project> [sNN]           # what an animator needs
uv run ivideo render <project> sNN            # 480p draft + layout report + frames
uv run ivideo render-all <project>            # 1080p
uv run ivideo assemble <project> [--burn-subtitles]
uv run ivideo status <project>
uv run ivideo mark <project> sNN approved
```

Every command prints JSON.

## 4. How the sync works

Audio comes first. `ivideo voice` speaks each line, cuts the silence at the ends,
and puts fixed gaps between lines (0.6 s lead-in, 0.35 s or 0.55 s between lines,
1.0 s at the end). It writes the start and end time of every line to `timing.json`.

Each scene is a `SyncedScene` (see `ivideo/synced.py`). The animator calls
`self.at_line(i)` before it animates line `i`. That call waits until the line
starts. `self.finish()` holds the last frame until the scene audio ends. So the
video of each scene has the same length as its audio, and each visual change
starts when its line starts.

After every animation, `SyncedScene` checks the layout:
text off the frame, text on top of other text, and text smaller than 20 pt.
It also records drift (animations that run past their line).
`ivideo render` returns these problems as JSON, so the agent can fix them.

## 5. The script format

`script.json` (from OpenRouter, checked by `ivideo/schema.py`):

```json
{
  "title": "...", "summary": "...",
  "speakers": [{"id": "host", "name": "Maya", "role": "teacher"},
               {"id": "learner", "name": "Sam", "role": "learner"}],
  "scenes": [{"title": "...", "learning_goal": "...", "visual_plan": "...",
              "lines": [{"speaker": "host", "text": "spoken words", "visual": "what is on screen"}]}]
}
```

Rules: at most 40 words per line (the prompt asks for 30), no markup, no stage
directions, every speaker used. The tool asks OpenRouter for strict JSON-schema
output. If the model cannot do that, it asks for JSON in the prompt. If the JSON
breaks a rule, it sends the problems back to the model (2 repairs at most).

## 6. Tests

```bash
uv run pytest        # 19 tests, about 30 s; no network and no key needed
uv run ruff check .
```

The OpenRouter tests use a local fake server.
`tests/fixtures/demo_script.json` is a hand-written test script, not OpenRouter output.

## 7. Limits

- 2D motion graphics only (Manim). No avatars, faces, or video footage.
- English voices only by default (Kokoro has some other languages).
- No background music.
- Render time is about 1 to 3 minutes per scene at 1080p on 4 CPUs.
- The reviewers judge still frames, not motion between frames.

See `RESEARCH.md` for how other systems do this and why this design was chosen.
