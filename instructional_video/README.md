# ivideo: animated instructional videos

This tool makes a short animated teaching video from a topic.

- **Claude Opus 5.5** (your Claude plan, effort high, through a Claude Code Workflow)
  writes the script: the spoken dialogue and the on-screen beat for each line.
- **OpenRouter** speaks each line with the free voice model
  `heygen/heygen-voice-1-turbo:free` (voices: nadine, tabitha, bram, gareth, noelle,
  meredith, sabrina, lucian, weston, vincent). Kokoro-82M is a local option with no key.
- **Claude Opus 5.5 agents** (same plan, effort high) write
  Manim animation code for each scene, render it, look at the frames, and fix problems.
- **ffmpeg** joins the scenes, the voice, and the subtitles into one MP4.

This folder is separate from SkyWeave. It does not touch `v1/` or `v2/`.

## Look: 3Blue1Brown-inspired

- Black background, white CMU Serif text (the LaTeX Computer Modern face).
- The classic Manim palette: BLUE #58C4DD, YELLOW #F7D96F, GREEN #83C167,
  RED #FC6255, TEAL, GOLD, PURPLE, PINK, MAROON, GREY.
- One color per concept, used the same way in drawings and in LaTeX equations.
- Equations morph with TransformMatchingTex. Faint NumberPlane grids. No slides,
  bullet lists, mascots, or branding.
- Centered titles with no underline. Set `IVIDEO_THEME=slate` for the older look.

## 1. Set up (one time per machine)

```bash
cd instructional_video
./setup.sh            # system packages incl. LaTeX and the CMU Serif font, Python env, Kokoro model
./setup.sh --no-latex # skip LaTeX (scenes then cannot use MathTex)
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
| `script_file` | Use this script JSON instead of writing one | none |
| `redo_script` | Write a new script for an existing project | false |
| `voice_engine` | `openrouter` or `kokoro` (local, no key) | openrouter |
| `tts_model` | OpenRouter speech model | `heygen/heygen-voice-1-turbo:free` |
| `redo_voice` | Speak all lines again | false |
| `redo_scenes` | Scene ids to animate again, for example `["s02"]` | none |
| `voices` | Voice per speaker, for example `{"host": "gareth"}` | host nadine, learner bram |
| `speed` | Speech speed | 1.0 |
| `max_fix_rounds` | Review-and-repair rounds per scene | 2 |
| `max_renders` | Renders per animator agent | 6 |
| `redo_design` | Write `scenes/common.py` again | false |
| `animator_model` | Model for every Claude agent | `claude-opus-5-5` |
| `effort` | Reasoning effort for every Claude agent | high |
| `burn_subtitles` | Draw subtitles into the picture | false (soft subtitles) |


What the workflow does:

Every Claude agent runs on `claude-opus-5-5` at effort `high`.

1. **Prepare** (1 agent): checks the setup and the OpenRouter key, and writes the brief.
2. **Script** (1 Claude Opus 5.5 agent): writes `script.json` with `ivideo script-rules`
   and checks it with `ivideo check-script` until it passes.
3. **Voice** (1 agent): OpenRouter `heygen/heygen-voice-1-turbo:free` speaks each line;
   the tool measures the time of each line. A line is spoken again only if its text,
   voice, or model changed.
4. **Design** (1 Claude Opus agent): writes `scenes/common.py`, the shared drawing
   library for this video. It holds a drawing function for each object that more
   than one scene uses, fixed positions, one color per concept, and a "handoff"
   function for each scene cut where the next scene continues the same diagram.
   It renders a gallery image of the library and checks it.
5. **Animate** (1 Claude Opus agent per scene, in parallel): writes `scenes/sNN.py`,
   renders a 480p draft, looks at one frame per line, and fixes problems.
6. **Review** (1 Claude Opus agent per scene, that did not write the code):
   looks at every frame and approves or rejects. A rejected scene goes to a
   repair agent, then to review again (at most `max_fix_rounds` times).
7. **Continuity** (1 Claude Opus agent): compares the last frame of each scene with
   the first frame of the next, and the look of all scenes. It can send at most
   3 scenes back for repair (each repair is reviewed again).
8. **Assemble** (1 agent): renders all scenes at 1080p 30 fps and joins them.
9. **Final check** (1 Claude Opus agent): checks length, audio level, streams,
   and that all scenes look like one video.

Agent count: about 5 + 2 to 4 per scene. A 6-scene video uses about 20 to 30 agents.

Output: `projects/<project>/out/final.mp4`, `final.srt`, `contact_sheet.png`.

Run it again with the same `project` to continue. Approved scenes stay as they are.

## 3. Commands (the workflow agents use these; you can too)

```bash
uv run ivideo doctor
uv run ivideo new <project> --topic "..." --minutes 3 --speakers 2 [--source notes.md]
uv run ivideo script-rules <project>          # rules + JSON schema for the script writer
uv run ivideo check-script <project>          # validate script.json
uv run ivideo voice <project> [--engine openrouter|kokoro] [--voice host=nadine]
uv run ivideo dialogue <project> [--model id] # optional: script from an OpenRouter LLM instead
uv run ivideo brief <project> [sNN]           # what an animator needs
uv run ivideo render <project> sNN            # 480p draft + layout report + frames
uv run ivideo still <project> common.py       # last frame of a plain Scene (class Gallery)
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

After every animation, `SyncedScene` checks the layout: anything off the frame,
text on top of other text, text smaller than 20 pt, and text in the subtitle band
(below y = -3.0). `self.allow_overlap(a, b)` marks an overlap as intended.
It also records drift (animations that run past their line).
`ivideo render` returns these problems as JSON, so the agent can fix them.
Before it renders, it refuses ManimGL names (for example `ShowCreation`),
`add_sound`, wall-clock calls, and random numbers without a fixed seed.

Scenes place objects on a 6x6 grid: `cell("A1")` is top-left, `cell("F6")` is
bottom-right (an idea from Code2Video, where a 6x6 grid gave the best layout score).

Assembly encodes the audio only once. Each scene gets a length of whole frames.
The video parts and the WAV parts are joined, then loudness is set to -16 LUFS
in two passes, then AAC is encoded one time. This keeps the voice in sync at
every scene cut. `out/assembly.json` has a `qa` block with automatic checks:
length, streams, loudness, long silences, and one subtitle per line.

## 5. The script format

`script.json` (written by the Claude Opus 5.5 Script agent, checked by `ivideo/schema.py`):

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
directions, every speaker used.

The optional `ivideo dialogue` command can ask an OpenRouter LLM for the script
instead. The workflow does not use it. How that request works (`ivideo/openrouter.py`):

- The tool reads the model's `supported_parameters` from `/api/v1/models` and sends
  only those. Claude 5.5 and GPT-6.1 Sol reject `temperature`, so it is not sent to them.
- It asks for strict JSON-schema output when the model supports it. If not, it puts
  the schema in the prompt.
- If a model fails, it tries the next model with a new request body (client-side fallback).
- If the JSON breaks a rule, it sends the problems back to the same model (2 repairs at most).
- A cut-off answer (`finish_reason: length`) is an error. 401, 402 and 403 stop at once.
- `openrouter_response.json` logs each call: served model, cost in USD, finish reason.
- `ivideo doctor` shows your remaining OpenRouter credit when the key is set.

## 6. Tests

```bash
uv run pytest        # 29 tests, about 30 s; no network and no key needed
uv run ruff check .
```

The OpenRouter tests use a local fake server. The render tests use the `tone` voice
engine (a quiet beep, no model needed).
`tests/fixtures/demo_script.json` is a hand-written test script.

## 7. Limits

- 2D motion graphics only (Manim). No avatars, faces, or video footage.
- English voices by default. The free HeyGen model may rate-limit; the tool retries.
- No background music.
- Render time is about 1 to 3 minutes per scene at 1080p on 4 CPUs.
- The reviewers judge still frames, not motion between frames.

See `RESEARCH.md` for how other systems do this and why this design was chosen.
