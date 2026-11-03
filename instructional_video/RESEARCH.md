# How LLM-made animated instructional videos are built (state of the art, 2026-11-03)

This report merges four research sweeps and their fact checks. Each claim has a source number [n]. Claims that the fact checkers refuted were removed or corrected. Claims that nobody could check are marked "unverified". Numbers marked "sandbox test" were measured during this research on a 4-CPU, 16 GB box with no GPU. They are not published benchmarks.

Terms used in this report:
- **LLM**: large language model, a model that writes text or code.
- **VLM**: vision language model, a model that reads images and text.
- **TTS**: text-to-speech, software that turns text into spoken audio.
- **RTF**: real-time factor. RTF = synthesis time divided by audio length. RTF 0.4 means 10 s of audio takes 4 s to make.
- **Manim CE**: Manim Community Edition, a Python library that draws 2D math and diagram animations.
- **Render**: the step that turns animation code into video frames.

## Summary

- Most open systems use the same chain. An LLM plans scenes. An LLM writes Manim code for each scene. A renderer makes the video. ffmpeg joins the parts [1][5][29].
- Manim CE is the main animation engine in research systems [1][5][19][29].
- Layout is the main failure that remains. Objects overlap or leave the frame [1][5][26].
- The best systems check layout two ways. A cheap code check finds boxes that overlap. Then a model looks at rendered frames [20][21][29].
- Code checks beat vision critics used alone. Human raters preferred a geometry check over a VLM critic in 65.0% of comparisons [21].
- Claude Opus is a strong animation coder. In Code2Video, Claude Opus 4.1 had the best scores of all tested models [5].
- The most reliable sync method is "audio first". The pipeline makes the speech first, measures it, and fits the animation to it [25][29][16].
- The closest existing match to our plan is Chalkboard (MIT license). It uses Claude agents, Manim CE, Kokoro voices, a layout dry run, and at most 2 frame-review rounds [29].
- OpenRouter can write the dialogue as strict JSON. Claude 5.5 models reject the `temperature` parameter, so the request must not send it [78][81].
- Kokoro-82M is the best free local voice for a CPU box. It ran at RTF about 0.4 on 4 CPUs in the sandbox test [99][103].
- Join the final video once. Join video-only scene files and one master WAV file. Then encode the audio one time. Per-scene AAC files drifted 21 to 26 ms after one join in the sandbox test [125][126].

## Research agent systems

### Overview table

| System | Stages | Animation engine | Sync method | Feedback loop | Link |
| --- | --- | --- | --- | --- | --- |
| TheoremExplainAgent (ACL 2025, oral) | Planner: outline, storyboard, technical plan, narration plan. Then one coder per scene | Manim CE (repo pins 0.18.1) | manim-voiceover with a custom Kokoro service; `run_time=tracker.duration` | Fix from error text, up to 5 tries. Optional visual fix has no hard cap (off by default) | [1][2][3][4] |
| Code2Video (Show Lab, NUS) | Planner, Coder (parallel per section), Critic | Manim CE v0.19.0 | None. The code makes no audio | ScopeRefine error repair. Gemini 2.5 Pro critic with a 6x6 anchor grid, 2 rounds | [5][6][7][8] |
| Paper2Video / PaperTalker | Slides, subtitles, speech, cursor, talking head | Beamer LaTeX slides | F5-TTS per slide. WhisperX word timestamps drive the cursor | Compile repair. A VLM picks one of several rendered layout variants | [9] |
| PresentAgent | Parse, slides, narration, TTS | python-pptx or HTML slides | Each slide stays on screen for its audio length | Not described | [10] |
| VideoAgent (ICMR '26) | Parse, analyze, plan, synthesize | python-pptx and Manim | Slides last as long as audio. Manim clips are time-scaled | Regenerate failed parts. A VLM checks sampled frames | [19] |
| TeachMaster (ACL 2026 Industry Track) | Composition, pages, coding, narration, TTS, debug, sync, layout | Manim | A sync agent inserts waits from code anchors and speaking rate | Repair loop, then a template fallback | [16][17] |
| OmniManim | Scene agent, trained layout model, code agent, repair agent | Manim | Not covered | Deterministic image checks. 1 repair round | [20] |
| SGA (KAUST) | Plug-in between the LLM and the render | Manim | Not covered | Partial run of the code, box check, small patch prompt. No render | [21] |
| PhysicsSolutionAgent | GPT-5-mini coder plus VLM critic | Manim | manim-voiceover with Kokoro | 1 round, 3 screenshots per scene | [22] |
| Manimator | Two LLM stages | Manim | None | None | [12][13] |
| Math-To-Manim | Brief, math dossier, storyboard, scene. A judge routes defects | Manim | None | Contact sheets. Gated mode sends each defect to the stage that caused it | [14] |
| Chalkboard | LangGraph Claude agents: script, fact check, code, code review, visual QA | Manim CE 0.21.0 | Cue markers `[[k]]` at words. TTS reports word times. Scene calls `self.cue(k)` | AST guard, layout dry run, at most 2 visual QA rounds, stop after 3 failed tries | [29] |
| manim-skill (Yusuke710) | Narration, TTS, code, render, mux, view | Manim CE | Kokoro first. Line durations become literal time budgets. A timing linter must pass | Lint, render, the agent reads frames | [25] |
| QuestionVideoExplainer | Plan, code and voiceover, lint and fix, render, evaluate | Manim | manim-voiceover with gTTS | Up to 5 fix tries. No vision step | [26] |
| video_explainer | Parse, script, TTS, storyboard, animation | Remotion (React) | TTS before the storyboard. Word timestamps drive timing | "AI visual inspection" (method not documented) | [27] |
| OpenMontage | Research to compose. The coding agent follows YAML manifests | Remotion, HyperFrames | WhisperX captions | Deterministic checks after render | [28] |
| videowright | Each beat is an HTML web component | HTML | TTS, then speech-to-text word timestamps align beats | Not checked | [30] |
| MoVer (SIGGRAPH 2025) | LLM writes the animation and a logic program that tests it | SVG motion graphics | Not covered | Predicate check, then LLM repair | [31][32] |

### Key results

**Code2Video [5][6][7][8]**
- With Claude Opus 4.1, the agent scored 87.9 aesthetics and 86.0 TeachQuiz. It used 13.8 min and 43.1K tokens per topic.
- Claude Opus 4.1 writing Manim directly scored 37.8 and 40.0. Direct-code rows exist only for GPT-5, GPT-4.1 and Claude Opus 4.1.
- TeachQuiz is a metric. A model takes a quiz before and after it "watches" the video.
- The 6x6 anchor grid gave the best layout score (65.6). An 8x8 grid scored 60.6. No grid scored 54.7.
- The full system took 15.4 min with GPT-4.1. Without parallel coding it took 86.6 min. Without parallel coding and ScopeRefine it took 149.8 min.
- ScopeRefine repairs in steps. First it fixes about 5 lines on each side of the error. Then it tries up to 3 whole-file fixes.
- Its dry run returns at the top of `construct()`. So it only finds import and setup errors.
- The authors say LLMs and VLMs "often fail to provide actionable feedback due to limited spatial awareness".
- They also say humans notice brief overlaps that video models miss.
- The README says Claude-4-Opus gave the best Manim code. The repo uses the MIT license.

**TheoremExplainAgent [1][2][3][4]**
- o3-mini had a 93.8% success rate and an overall score of 0.77.
- Claude 3.5 Sonnet v1 succeeded only 2.1% of the time (14.6% with retrieval). It still had the top overall score, 0.79. The overall score counts only videos that rendered.
- Cost per video was $0.10 to $4.67. Time per video was about 1120 s to 2380 s.
- Automatic scores agree poorly with humans for 3 of 5 measures (Spearman 0.14, 0.16, 0.17).
- The coder prompt sets a 0.5-unit safe margin and a 0.3-unit minimum gap between objects.
- The visual-fix loop resets its retry counter after each revision. So that loop has no hard cap.
- Every attempt renders at `-qh` (1080p, 60 fps). This is slow on a CPU.
- The Kokoro cache key leaves out speed. So a speed change can reuse old audio [4].
- The authors say most videos have small layout problems.

**SGA [21]**
- SGA runs the Manim code in parts and reads each object's box. It does not render.
- This takes under 5 s. A render plus VLM pass takes 30 to 90 s.
- Raters preferred SGA over the raw output in 84.4% of comparisons. They preferred it over a VLM critic in 65.0%.
- Overlap is "Medium" above 10% box coverage and "High" above 30%.
- It checks only settled states (start and end of each animation). It ignores overlaps during motion.
- VLM critics sometimes pushed objects off the frame. They also "fixed" overlaps that were intentional.
- In 1 of 8 setups, SGA lowered the score (63.84 to 62.83).
- The paper says the code is public. The project page has no code link. Treat it as not released.

**Other results**
- OmniManim scored R@1 0.907 in 72 s. Code2Video scored 0.841 in 148 s. Both used GPT-5.4. Humans gave 81.0 against 68.4. The code is not released [20].
- VideoAgent Table 3 reports Claude-4.5-Opus as a Manim coder: Pass@1 90%, Pass@3 100%. It cost about $0.35 per video with Gemini 2.5 Pro [19].
- VideoAgent states a 3-turn cap only for its Manim test. Its main algorithm has no stated cap [19].
- PhysicsSolutionAgent used one screenshot critique round. Layout quality moved only from 3.64 to 3.66 [22].
- Chalkboard reports Claude cost of about $0.40 to $1 per 1 to 2 min topic. A 5:54 lecture cost $3.10 [29].
- Manimator scored 0.845 on automatic metrics. Humans rated its layout 0.52 and its overall quality 0.738. Its venue was an ICML 2025 "New In ML" poster, not a workshop [12][13].
- TeachMaster reports a production time ratio of 2.46 against 24.46 for humans. A 45-hour course cost about $83.70. It released no code [16][17].
- PaperTalker cut time from 287.2 min to 48.1 min with parallel slides (about 6x). It ran on eight RTX A6000 GPUs [9].
- MoVer reports 58.8% correct with no repair and 93.6% with up to 50 repair rounds [31].
- ManiBench found that models confuse ManimGL and Manim CE (for example `ShowCreation`, `CONFIG`). Claude-Sonnet-4 tied for the highest zero-shot pass rate, 0.667 (8 of 12) [24].
- Correction: ManiBench does not show that version-aware prompts remove version errors. The only 0.000 result was for a model already at 0.000 [24].
- Math-To-Manim routes each defect to the stage that caused it. It records a missing LaTeX or FFmpeg as an environment error with zero repair calls [14].
- Correction: Math-To-Manim's main pipeline uses the OpenAI Codex SDK. Its judge reads text only. The Claude contact-sheet review is a showcase script, not the main tool [14].
- LLM2Manim keeps a "symbol ledger" so notation stays the same in all scenes. Students scored 83% against 78% for PowerPoint (d=0.67) [23].
- LASEV claims more than 1M videos per day. It gives no quality figure and no code [18].
- Only one Motion Canvas agent was found: a small demo with no documented internals (low confidence) [132].

**Licenses**: TheoremExplainAgent, Code2Video, Paper2Video, manim-skill, Math-To-Manim and Chalkboard use MIT [1][5][9][25][14][29]. OpenMontage uses AGPL-3.0 [28]. QuestionVideoExplainer and video_explainer have no LICENSE file, so do not copy their code [26][27].

## Tools and libraries

### Manim Community Edition (primary engine)
- The latest version is 0.22.1 (2026-10-27). It needs Python 3.11 or newer. The license is MIT [33].
- Version 0.19.1 was the last version for Python 3.10 [33].
- The default renderer is Cairo. Cairo draws on the CPU, so Manim needs no GPU and no display [37].
- manimpango and pycairo have no Linux wheels. Linux needs `build-essential python3-dev pkg-config libcairo2-dev libpango1.0-dev` [34].
- LaTeX is optional. Version 0.21.0 added Typst math (`manim[typst]`), which needs no TeX [35].
- Since 0.19.0, Manim calls ffmpeg through PyAV. A system ffmpeg is still needed for joining files [39].
- `-ql` gives 854x480 at 15 fps. `-qh` gives 1920x1080 at 60 fps. `-s` saves the last frame as PNG. `--format png-sequence` saves every frame [36].
- The built-in default is 1080p at 60 fps. An agent that forgets `-ql` makes a slow draft [37].
- `Scene.time` gives the time since the scene started. `Scene.add_sound()` places audio at a time [38].

### manim-voiceover
- Version 0.4.0 came out on 2026-06-14 after 20 months with no release. It needs Python 3.11 or newer. Its status is "3 - Alpha" [40].
- It adds `with self.voiceover(text=...) as tracker:` blocks. Animations use `tracker.duration` [41].
- Bookmarks trigger animations at named words. They need word timings. Only the Azure service gives these natively. Other services need a Whisper pass [40].
- Its extras pin `openai<2` and `elevenlabs<0.3`. These conflict with current SDKs [40].
- TTS runs inside the render. So every re-render is tied to TTS.

### Remotion
- Remotion uses React. It renders frames in headless Chrome. The latest version is 4.0.540 [44].
- It has official agent skills and an official Claude Code plugin [42][43].
- It is source-available, not open source. It is free only for individuals and companies of up to 3 people [44].
- Bigger companies pay $25 per seat per month, or $0.01 per render with a $100 monthly minimum [44].
- Linux needs glibc 2.35 or newer and some Chrome libraries [46].
- Its LLM system prompt forbids `Math.random()` and asks for seeded random values [45].

### HyperFrames (HeyGen)
- HyperFrames turns HTML plus seekable animation (GSAP, CSS, Lottie and others) into MP4 [47].
- It uses Apache-2.0. It needs Node 22 or newer and FFmpeg [47].
- It has an official Claude Code plugin with 9 skills [47].
- It is pre-1.0. Version 0.9.9 came out on 2026-11-03. Pin a version [47].
- GSAP has a proprietary no-charge license with a clause against competing visual builders [48].

### Other engines
- Motion Canvas has had no stable release since 3.17.2 (2024-12-14). It is built around a browser editor. Do not use it for unattended work [49].
- Revideo (MIT) can render headless. It sends telemetry unless `DISABLE_TELEMETRY=true` [50].

### Claude Code skills for video
- Yusuke710/manim-skill (MIT): audio first, Kokoro, timing linter, 854x480 at 15 fps drafts [25].
- DominikPeters/manim-skill: PNG frames and contact sheets. It has 1 star, so treat it as anecdotal [52].
- digitalsamba/claude-code-video-toolkit (MIT, about 2.4k stars): Remotion based. It uses cloud GPUs for some assets [51].
- awesome-manim-skills lists more skills. It advises Cairo, not the experimental OpenGL renderer [53].
- NousResearch hermes-agent ships a manim-video skill [54].

## Commercial products

| Product | What it makes | Public facts about how it works | Lesson for us |
| --- | --- | --- | --- |
| Claude Motion (Anthropic, beta) | Animated text, charts, shapes and images. MP4 export only | Claude writes animation code. No video-generation model. No documented API or narration. Team and Enterprise plans | Confirms the "LLM writes code, renderer makes MP4" route. Not usable as a part [55] |
| Claude Motion launch (Runway post) | Launch on 2026-10-08 | Voiceover and music are added in Runway | Motion itself does no narration [56] |
| NotebookLM Video Overviews | Narrated videos from user sources | Oct 2025: Brief and Explainer formats, 6 styles with Nano Banana images. Mar 2026: Cinematic with Gemini 3 as "creative director", Nano Banana Pro and Veo 3 | Planner makes structural choices before visuals [57][58] |
| NotebookLM help page | Formats now: Cinematic, Explainer, Short (about 60 s) | Focus prompt. Producer-notes review for Ultra subscribers. Generation can take 30+ min | Format presets and a review step before render [59] |
| Synthesia 3.0 | Avatar presenter videos | Express-2 avatars, voice cloning, Video Agents, Veo 3 B-roll | Not a fit for diagrams [60] |
| HeyGen Video Agent API | Avatar videos with motion graphics | `POST /v3/video-agents`. Graphics use HyperFrames. Render takes 5 to 10 times the video length. Chat mode pauses for review | A review gate before render [61] |
| Vyond Go | Template explainer drafts | Prompt up to 4,000 characters. Unnamed "LLM partners" | Pick from tested templates [62] |
| Colossyan | Avatar training videos | Agent "Cora". Feature page: "Nothing renders until you approve". Early access | Approve the storyboard before CPU-heavy renders [63] |
| Pictory | Stock-footage videos | The agent drafts scenes. The user picks stock visuals. ElevenLabs voices | Low relevance [64] |
| invideo | Generative ads and social video | "Agentic video editor". Connects outside agents. Uses many video models | Low relevance [65] |
| Google Vids | Workspace videos | Support page confirms auto AI voiceover and a 2,500-character script limit. Storyboard, avatar and Veo details come only from press (unverified) | Low relevance [66] |

None of these products publishes its internal pipeline. Statements beyond their own pages are unverified.

## OpenRouter for dialogue

### API facts
- The endpoint is `POST https://openrouter.ai/api/v1/chat/completions` [67].
- Send `Authorization: Bearer <OPENROUTER_API_KEY>`. The OpenAI SDK works with `base_url="https://openrouter.ai/api/v1"` [67].
- Optional headers: `HTTP-Referer`, `X-OpenRouter-Title` (the old `X-Title` still works) and `X-OpenRouter-Categories` [68].
- `X-OpenRouter-App-Visibility: hidden` keeps a new app off the public rankings [68].
- Strict JSON output uses `response_format: {type: "json_schema", json_schema: {name, strict: true, schema}}` [69].
- Support for strict JSON is per provider endpoint, not per model. Enforcement "varies by provider" [69].
- `provider.require_parameters: true` sends the request only to endpoints that support every parameter in it [71].
- With the default (`false`), providers ignore unknown parameters. `response_format` then acts as a soft preference [71].
- The `models` array gives fallbacks in priority order. Billing uses the model that served the request. The response field `model` names it [72].
- The Response Healing plugin repairs broken JSON. It cannot repair output that was cut off by the token limit [70].
- Every response includes usage: tokens, `cost`, cached tokens and reasoning tokens [73].
- Cost is in credits. Credits are US dollars [74].
- Free models allow 20 requests per minute. Paid models have no platform-level request cap [75].
- OpenRouter holds the estimated cost (input plus `max_tokens`) before the call. Low balances can get error 402 during parallel calls [75].
- The listed model price is the "top provider" price. It can carry overrides for long prompts and time windows [76].
- Correction: the listed price is not always the cheapest endpoint. For GLM-5.3, one fp4 endpoint is cheaper than the listed price [79].
- Cheaper "flex" and faster "priority" endpoints are opt-in. Use `service_tier`, `:floor` or `:nitro` [71].
- `GET /api/v1/models` is public. On 2026-11-03 it listed 454 text models [77].
- `GET /api/v1/models/{id}/endpoints` shows each endpoint's supported parameters [78].
- Reasoning tokens are billed as output. On most providers they count against `max_tokens` [80].
- Pin exact model ids. Aliases such as `~anthropic/claude-opus-latest` change targets [77].

### Claude 5.5 rules
- Anthropic's guide says Opus 5.5 returns error 400 for `temperature`, `top_p`, `top_k`, `budget_tokens`, forced `tool_choice` and assistant prefill [81].
- Thinking is always on. Anthropic's native default effort is "medium". OpenRouter's listing says "high". Set the effort explicitly [80][81].
- No OpenRouter endpoint of claude-opus-5.5, claude-sonnet-5.5 or gpt-6.1-sol lists `temperature`. Claude 5.5 endpoints also lack `seed` [78].
- Amazon Bedrock endpoints of Opus 5.5 and Sonnet 5.5 lack strict JSON output [78].
- A user report shows Bedrock returning 400 "`temperature` is deprecated for this model" [82].

### Recommended models (prices on 2026-11-03, USD per million tokens, input / output)

| Role | Model id | Price | Evidence |
| --- | --- | --- | --- |
| Default | `anthropic/claude-opus-5.5` | $4 / $20 | Top of the Artificial Analysis index on OpenRouter rankings (58.4, measured at "Max" effort) [83]. EQ-Bench longform 84.6 [84] |
| Cheaper option | `anthropic/claude-sonnet-5.5` | $2 / $10 | Index 54.2 [83]. Current default in our code |
| Fallback 1 | `openai/gpt-6.1-sol` | $2 / $10 ($4 / $15 above 272k prompt tokens) | Other vendor. Index 54.5 [83][77] |
| Fallback 2 | `x-ai/grok-4.7` | $2 / $6 | Third vendor. Longform 83.0 with the lowest "slop" score (4.88) of the top models [84][77] |
| Not preferred | `google/gemini-4-argon` | $2 / $10 | Longform slop score 16.70 [84] |

- EQ-Bench tests fiction. Its judge has been Claude Sonnet 4.6 since 2026-02-19, so Claude models may get a bias [84][85].
- No benchmark tests instructional dialogue. Run a small A/B test on our own topics.
- A model-list benchmark (Design Arena, "codetovideo") ranks claude-opus-5.5 first (Elo 1354). Medium confidence, from the models API [77].
- Our estimate: one 3-minute script needs about 20k input and 5k to 15k output tokens. That is about $0.20 to $0.40 on Opus. This is an estimate, not a measurement.

### Local finding in our code (fixed)
- The first version of `ivideo/openrouter.py` always sent `temperature` and used one shared `models` fallback array [L1].
- So a live call to a Claude 5.5 model would probably have fallen back silently to another model.
- Fixed in this build: each request is built from the model's `supported_parameters`. `temperature` and `seed` go only to models that list them. Fallback is client-side, one body per model. `finish_reason: length` is an error. 401/402/403 stop at once. Each call logs the served model, the cost and the finish reason. Tests cover each rule with a local fake server.

### OpenRouter audio
- `POST /api/v1/audio/speech` gives TTS with the same key. It was announced on 2026-05-01 [86][88].
- On 2026-11-03 it listed 38 speech models. Examples: `hexgrad/kokoro-82m` ($0.62 per million characters) and `elevenlabs/eleven-v4` ($80 per million characters) [87].
- It returns audio bytes with no timestamps [86].
- Multi-speaker input works only for Gemini TTS and Fish Audio (not S1). Other providers return 400 [86].
- Gemini TTS is billed per token. Some models are billed per second [86].
- `POST /api/v1/audio/transcriptions` can return word-level times with `timestamp_granularities=["word"]` (from a checker; not tested) [89].
- An official cookbook makes a two-voice podcast from strict-JSON turns [90].
- Chat completions can also stream audio from `openai/gpt-audio` models [91].

### What the script should contain
- Median engagement was 6 minutes or less, for any video length [92].
- Correction: for tutorials, Guo et al. advise support for re-watching and skimming. They do not advise a 2 to 3 minute cap [92].
- Speaking rates ranged from 48 to 254 words per minute (mean 156) [92].
- Correction: the 145 to 165 wpm group was the least engaging for 6 to 12 minute videos. So do not aim for that range as a target. The authors think enthusiasm, not speed, explains the effect [92].
- Learners who heard a virtual student ask deep questions wrote more relevant recall. No other differences were significant [93].
- A video dialogue that voices misconceptions beat a lecture-style video [94].
- Correction: a single narrator who states and refutes a misconception worked about as well (d=0.79 against d=0.83) [95].
- So two voices are optional. The active part is naming and correcting the misconception.
- Pairs who watched tutoring together learned as well as the tutees. A single viewer is a weaker case [96].
- Mayer's principles: show words and pictures together, cue the structure, and do not repeat the narration as on-screen text [97].
- Podcastfy shows a simple config for roles and structure. Its default "questioner" role is weaker than the research above [98].

## Text-to-speech options

| Engine | License | CPU speed | Word timing | Voices | Verdict |
| --- | --- | --- | --- | --- | --- |
| kokoro-onnx 0.6.1 with Kokoro v1.0 | Wrapper MIT, weights Apache-2.0 | RTF 0.39 to 0.44 (sandbox test, reproduced at 0.40) | Phoneme times from `create_timed()`. Group on spaces to get words. Fragile on numbers | 54 voices in 9 languages | **Default** [99][103][104] |
| kokoro 0.9.4 (PyTorch) | Apache-2.0 | RTF 0.51 to 0.62 (sandbox test). 1.3 GB venv | English tokens carry `start_ts` and `end_ts` | Same | Use only if word timing must match the text exactly. Needs Python below 3.13 [101][102] |
| Piper (piper-tts 1.8.0) | Engine GPL-3.0. Voice licenses vary | RTF 0.05 to 0.065 (sandbox test) | Experimental phoneme alignments | Many | Fast drafts only [105][106][107] |
| XTTS-v2 (coqui-tts 0.28.0) | Weights non-commercial | Slow on CPU (one low-reliability report) | None | Voice cloning | Avoid [110] |
| edge-tts 7.3.1 | Client LGPL-3.0. Service terms unclear | Network service | WordBoundary events | 322 voices (47 English) | Opt-in only [111][112] |
| OpenRouter speech | Per model | Hosted | None documented | 38 models | Paid upgrade [86][87] |
| ElevenLabs API | Paid. Free plan is non-commercial and needs credit | Hosted | Character times. A dialogue endpoint returns per-line segments | Many | Best paid option when timing matters [114][115][116] |
| OpenAI TTS (gpt-4o-mini-tts, tts-1) | Paid | Hosted | None | 13 | Avoid. Shuts down on 2027-01-06 [113] |
| Supertonic 3 | OpenRAIL-M | RTF 0.165 (community test, 4 vCPU) | Total length only | 10 | Watch list [117][118] |

Notes:
- Kokoro voice grades: only `af_heart` is "A". `af_bella` is "A-". The best US male voices (`am_michael`, `am_fenrir`, `am_puck`) are "C+". `bf_emma` is "B-" [100].
- `create_timed()` returns empty timings if the model has no duration output. Release `model-files-v1.1` states that all its models have it [104].
- Our `setup.sh` downloads from `model-files-v1.0` [L3]. This is fine for `create()`. Switch to v1.1 before using word timings.
- The kokoro PyTorch package tries to download a spaCy model at run time. This fails in a uv venv unless you install it first (sandbox test) [102].
- Piper `en_US-lessac` uses research-only data. `ryan-high` is non-commercial [109][108].
- Correction: Piper `libritts_r` is CC BY 4.0, but it was fine-tuned from lessac. Its commercial status is uncertain [108].
- Audio made by a GPL program is generally not covered by the GPL. This is not legal advice [129].
- Kokoro-FastAPI runs a local OpenAI-style speech server. Its captioned endpoint returns word times [119].
- Aligners: WhisperX can align known text on CPU. Numbers and currency get no timing [120]. stable-ts is archived [121]. aeneas last shipped in 2017 [122].
- Python window: Manim 0.22.1 needs 3.11 or newer. kokoro-onnx needs below 3.14. PyTorch kokoro needs below 3.13. Use Python 3.12 [33][103][102].
- Newer onnxruntime releases also need Python 3.11 or newer [124].
- If PyTorch is needed, install it from the CPU wheel index. Otherwise uv pulls large CUDA wheels [123].

## Audio and video sync methods

Four patterns exist.

1. **Slide equals audio.** Each slide stays on screen for its audio length. PresentAgent and VideoAgent use this [10][19]. It is simple but static.
2. **TTS inside the render.** manim-voiceover makes audio during the render. Animations use `tracker.duration`. TheoremExplainAgent, QuestionVideoExplainer and PhysicsSolutionAgent use this [41][1][26][22]. Each re-render repeats or reloads TTS.
3. **Audio-first manifest.** TTS runs first for each line. The pipeline writes measured start and end times to a file. The animation code must fit those times.
   - manim-skill turns durations into literal time budgets and lints them before render [25].
   - Chalkboard puts cue markers at words and waits for their spoken times [29].
   - TeachMaster inserts waits from speaking rate and code anchors [16].
4. **Word alignment after TTS.** A speech recognizer finds word times in the audio.
   - PaperTalker uses WhisperX [9]. videowright uses speech-to-text timestamps [30].

VideoAgent also stretches rendered Manim clips to fit narration. This distorts motion [19]. Fit `run_time` to audio instead.

Our choice is pattern 3. Line boundaries come free, because each line is a separate TTS call. Word cues are optional. They come from Kokoro phoneme times grouped into words.

## Assembly with ffmpeg

Tested on ffmpeg 6.1.1 in the sandbox [125][126].

1. For each scene, find T = max(video length, audio length). Round T up to whole frames: NF = ceil(T x fps), then T = NF / fps.
2. Pad the video with copies of its last frame. Write exactly NF frames with no audio:
   `ffmpeg -i anim.mp4 -vf "tpad=stop_mode=clone:stop=-1,fps=30,format=yuv420p" -frames:v NF -an -c:v libx264 -preset medium -crf 18 -tune animation -g 60 vNN.mp4`
3. Pad the audio with silence to exactly T, as WAV:
   `ffmpeg -i sceneNN.wav -af "aresample=48000,apad=whole_dur=T,atrim=end=T" -c:a pcm_s16le aNN.wav`
4. Join the video files: `ffmpeg -f concat -safe 0 -i vlist.txt -c copy video_all.mp4`. Join the WAV files the same way.
5. Measure loudness: `ffmpeg -i audio_all.wav -af loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json -f null -`
6. Mux once. Feed the measured values back to `loudnorm` with `linear=true`, then `aresample=48000`. Encode AAC once. Add soft subtitles with `-c:s mov_text`. Add `-movflags +faststart`.

Facts behind these steps:
- The concat demuxer needs the same codecs and settings in every file [126].
- Correction: the concat filter also needs matching parameters. It only adapts pixel format and audio format [125].
- Per-scene AAC files joined with stream copy drifted 21 to 26 ms after one join (sandbox test).
- A WAV master without frame rounding still drifted 33 ms. The frame rounding in step 1 is required (sandbox test).
- `loudnorm` defaults are I=-24, LRA=7, TP=-2. Its linear mode falls back to dynamic mode if the target peak or range cannot be met [125].
- Raw Kokoro audio measured -26.1 LUFS. LUFS is a unit of perceived loudness. loudnorm used dynamic mode on it (sandbox test).
- Apple recommends about -16 LKFS with true peak at or below -1 dBFS for spoken audio [128].
- Most browsers play only `yuv420p`. `-tune animation` suits flat-color content. CRF near 18 is a good quality level [127].
- `+faststart` moves the index to the start of the file for web playback. It is not on by default [126].
- Burned subtitles use `-vf subtitles=final.srt:force_style=...`. This needs an ffmpeg built with libass.
- ffmpeg-normalize wraps the two-pass loudness step [130]. pyloudnorm can match loudness per line in Python [131].

Local finding: our `ivideo/assemble.py` encodes AAC per scene and then joins with stream copy [L2]. This is the pattern that drifted in the sandbox test. Its scene lengths are also not rounded to whole frames.

## Common failure modes and how others fix them

| Failure | Evidence | How others fix it |
| --- | --- | --- |
| Objects overlap or leave the frame | Most common remaining problem [1][5]. QuestionVideoExplainer lists it as a known limit [26] | Numeric margins [2]. Anchor grid [5]. Geometry check before render [21][29]. Frame review after render [5][29] |
| Invented or wrong Manim API calls | Most common error type [1]. ManimGL and CE mixed up [24] | Pin the version in the prompt. Ban ManimGL idioms. Give the agent local docs [24][52] |
| LaTeX errors | Common error type [1] | Use Text, MarkupText or Typst. Install LaTeX only when needed [35] |
| Missing tools (LaTeX, ffmpeg) | [14] | Record an environment error. Do not ask the model to fix it [14] |
| Repair loops with no end | Retry counter resets after each visual fix [3] | Hard caps: 2 visual rounds and stop after 3 failed tries [29]. 1 repair round [20]. 2 critic rounds [6] |
| VLM critic misses brief overlaps or over-corrects | [5][21] | Run deterministic checks first. Check settled states only. Cap the rounds [21][22] |
| Slow renders | Every try at 1080p60 [3]. More than 2 hours without optimization [5] | Draft at low quality. Render scenes in parallel. Render the final quality once [5][25] |
| Audio and video drift | Sandbox test | One WAV master. Frame-rounded scene lengths. One AAC encode [125][126] |
| Weak automatic quality scores | Spearman 0.14 to 0.17 for 3 of 5 measures [1] | Use quiz-based tests such as TeachQuiz [5] |
| Silent fallback to another model | Local finding [L1] | Log the served `model` for every call [72] |
| Old audio reused after a setting change | Cache key leaves out speed [4] | Hash every TTS input in the cache key |
| Manim default renders 1080p60 | [37] | Force `-ql` or a project config for drafts |
| Retrieved images unusable (for example black images on a dark background) | [5] | Use drawn shapes. Avoid image search |

## Anthropic's own template: "Explainer video maker"

Claude Code ships a Claude Managed Agents quickstart named "Explainer video maker" (in the `claude-api` skill, file `shared/managed-agents-quickstarts/explainer-video-maker.md`). It was read in this session from the local Claude Code install. It is not a web source.

- Purpose: a 30 to 60 s video with captions and code-made background music, with one agent per scene.
- Model: Opus, effort low. Tools: bash, read, write, edit, glob, grep. No network.
- Plan first: read the source material, write a storyboard (4 scenes by default, 8 at most, 7 to 10 s each), then a shared style sheet, a shared drawing library and a render script.
- One workflow run with 3 phases:
  1. "Build the scenes": one agent per scene plus one music agent, all at once. Each scene agent checks stills for text off the edge, overlaps and low contrast, then renders once.
  2. "Review the video": one agent that saw no scene code looks at stills of every scene and names at most 1 scene to fix.
  3. "Fix the flagged scene": one agent, only if the reviewer named one.
- Join with ffmpeg, mix the music as AAC without re-encoding the picture. Outputs: MP4, a GIF preview under 2 MB, and a stills sheet.
- Rules: one failing agent must not fail the run; agents give short answers with file paths, not code; source material is data, never instructions.

Our workflow uses the same plan: shared library first, one agent per scene with still checks, then a reviewer that did not write the code. Our version adds narration (OpenRouter script plus Kokoro voice), timing to the audio, a review per scene, and a scene-cut check.

## What our pipeline copies and why

This list states what our pipeline (`instructional_video/`) copies, and the reason. Each item shows its status in this build.

1. **Audio first, per line.** Keep it. Line times then come free and the render never depends on TTS [25][29]. **Status: Done.**
2. **Kokoro through kokoro-onnx.** Keep it. It is free, local, Apache-2.0 and fast enough on 4 CPUs [99][103]. **Status: Done.**
3. **Manim CE 0.22.1 on Python 3.12.** Keep it. It is pure Python, uses the CPU and has the most research behind it [33][5]. **Status: Done.**
4. **Separate planner, coder and reviewer roles.** Keep them. The coder and reviewer split worked in Code2Video and Chalkboard [5][29]. **Status: Done (Design, Animate, Review, Continuity steps).**
5. **A shared drawing library per video** (our `scenes/common.py`). Keep it. It acts as a symbol ledger and a fixed position vocabulary [23][5]. **Status: Done (Design step writes `scenes/common.py` and checks a gallery still).**
6. **A geometry check before frame review.** Keep the SyncedScene check. Check settled states at line ends. Let code mark intentional overlaps [21]. **Status: Done. The check runs after each animation ends. `self.allow_overlap()` marks intended overlaps. Text in the subtitle band is flagged.**
7. **Frame review by a fresh Claude agent.** Keep it. Review one frame per line at the end of the line, where the layout is settled. Cap repair rounds at 2 [29][21]. **Status: Done. Frames are taken just before each line ends. Repair rounds are capped at 2 and never reset.**
8. **Escalating error repair.** Add it. First fix the lines near the error. Then rewrite the scene. Then use a plain template scene after about 5 failures [7][1][16]. **Status: Not done (deferred). Agents fix from the traceback. There is no template fallback scene yet.**
9. **Environment errors are not model errors.** Add it. `ivideo doctor` finds them before any agent runs [14]. **Status: Done. Prepare runs `ivideo doctor` and stops on setup errors.**
10. **Storyboard approval gate.** Add it as an option. A person approves the script before CPU-heavy renders [63][61]. **Status: Not done (deferred). Use the `script_file` argument to run a script you checked.**
11. **OpenRouter request fixes.** Add them. Build each request from the model's listed parameters. Drop `temperature` and `seed` for Claude 5.5. Log the served model and cost [78][81][72]. **Status: Done (see "Local finding in our code").**
12. **One-pass assembly.** Add it. Use a WAV master, frame-rounded scene lengths and one AAC encode [125][126]. **Status: Done. Video-only segments and PCM WAV segments with frame-rounded lengths, one AAC encode, two-pass loudnorm.**
13. **Deterministic final checks.** Add them. Check durations, streams, loudness, silence and subtitle count [28]. **Status: Done. `assembly.json` has a `qa` block: duration, streams, loudness, long silences, subtitle count.**
14. **Chapter titles per scene.** Add them. They help viewers skim and re-watch [92]. **Status: Partly done. Each scene shows its title. MP4 chapter markers are not written yet.**
15. **Do not copy:** manim-voiceover as a dependency [40], time-stretched clips [19], uncapped visual loops [3], or code from repos with no license [26][27]. **Status: Followed.**

## Sources

1. https://arxiv.org/html/2502.19400v2
2. https://github.com/TIGER-AI-Lab/TheoremExplainAgent/blob/main/task_generator/prompts_raw/prompt_code_generation.txt
3. https://github.com/TIGER-AI-Lab/TheoremExplainAgent/blob/main/src/core/video_renderer.py
4. https://github.com/TIGER-AI-Lab/TheoremExplainAgent/blob/main/src/utils/kokoro_voiceover.py
5. https://arxiv.org/html/2510.01174v1
6. https://github.com/showlab/Code2Video/blob/main/src/agent.py
7. https://github.com/showlab/Code2Video/blob/main/src/scope_refine.py
8. https://github.com/showlab/Code2Video
9. https://arxiv.org/html/2510.05096v2
10. https://arxiv.org/html/2507.04036v1
11. https://arxiv.org/abs/2505.21497
12. https://arxiv.org/html/2507.14306v1
13. https://icml.cc/virtual/2025/50491
14. https://github.com/HarleyCoops/Math-To-Manim
15. https://github.com/marcelo-earth/generative-manim
16. https://arxiv.org/html/2601.04204v2
17. https://aclanthology.org/2026.acl-industry.7/
18. https://arxiv.org/abs/2602.11790
19. https://arxiv.org/html/2509.11253v2
20. https://arxiv.org/html/2605.15585v1
21. https://arxiv.org/html/2607.18116v2
22. https://arxiv.org/html/2601.13453
23. https://arxiv.org/abs/2604.05266
24. https://arxiv.org/html/2603.13251v1
25. https://github.com/yusuke710/manim-skill/blob/main/skills/manim-skill/SKILL.md
26. https://github.com/stevencao2005/QuestionVideoExplainer
27. https://github.com/prajwal-y/video_explainer
28. https://github.com/calesthio/OpenMontage
29. https://github.com/nicglazkov/Chalkboard
30. https://github.com/scosman/videowright
31. https://arxiv.org/abs/2502.13372
32. https://pypi.org/project/mover/
33. https://pypi.org/project/manim/
34. https://docs.manim.community/en/stable/installation/uv.html
35. https://github.com/ManimCommunity/manim/releases/tag/v0.21.0
36. https://docs.manim.community/en/stable/guides/configuration.html
37. https://github.com/ManimCommunity/manim/blob/main/manim/_config/default.cfg
38. https://docs.manim.community/en/stable/reference/manim.scene.scene.Scene.html
39. https://docs.manim.community/en/stable/changelog/0.19.0-changelog.html
40. https://pypi.org/project/manim-voiceover/
41. https://github.com/ManimCommunity/manim-voiceover
42. https://www.remotion.dev/docs/ai/skills
43. https://www.remotion.dev/docs/ai/claude-code-plugin
44. https://www.remotion.pro/license
45. https://www.remotion.dev/system-prompt.txt
46. https://www.remotion.dev/docs/miscellaneous/linux-dependencies
47. https://github.com/heygen-com/hyperframes
48. https://gsap.com/standard-license/
49. https://registry.npmjs.org/@motion-canvas/core
50. https://github.com/redotvideo/revideo
51. https://github.com/digitalsamba/claude-code-video-toolkit
52. https://github.com/DominikPeters/manim-skill
53. https://github.com/marcelo-earth/manim-awesome-skills
54. https://github.com/NousResearch/hermes-agent/tree/main/skills/creative/manim-video
55. https://support.claude.com/en/articles/17454997-get-started-with-claude-motion
56. https://runway.com/news/company-news/runway-claude-motion
57. https://blog.google/innovation-and-ai/products/notebooklm/generate-your-own-cinematic-video-overviews-in-notebooklm/
58. https://blog.google/technology/google-labs/video-overviews-nano-banana/
59. https://support.google.com/notebooklm/answer/16454555
60. https://www.synthesia.io/post/synthesia-3-0-the-next-era-of-video
61. https://developers.heygen.com/docs/video-agent
62. https://www.vyond.com/product/vyond-go/
63. https://www.colossyan.com/features/ai-video-generator
64. https://pictory.ai/
65. https://invideo.io/
66. https://support.google.com/docs/answer/15070345
67. https://openrouter.ai/docs/api_reference/authentication.md
68. https://openrouter.ai/docs/app-attribution.md
69. https://openrouter.ai/docs/guides/features/structured-outputs.md
70. https://openrouter.ai/docs/guides/features/plugins/response-healing.md
71. https://openrouter.ai/docs/guides/routing/provider-selection.md
72. https://openrouter.ai/docs/guides/routing/model-fallbacks.md
73. https://openrouter.ai/docs/use-cases/usage-accounting.md
74. https://openrouter.ai/docs/faq.md
75. https://openrouter.ai/docs/api-reference/limits.md
76. https://openrouter.ai/docs/models.md
77. https://openrouter.ai/api/v1/models
78. https://openrouter.ai/api/v1/models/anthropic/claude-opus-5.5/endpoints
79. https://openrouter.ai/api/v1/models/z-ai/glm-5.3/endpoints
80. https://openrouter.ai/docs/use-cases/reasoning-tokens.md
81. https://platform.claude.com/docs/en/models/opus-5-5/migration-guide
82. https://github.com/orgs/community/discussions/209995
83. https://openrouter.ai/rankings
84. https://eqbench.com/creative_writing_longform.html
85. https://eqbench.com/creative_writing.html
86. https://openrouter.ai/docs/guides/overview/multimodal/tts.md
87. https://openrouter.ai/api/v1/models?output_modalities=speech
88. https://openrouter.ai/blog/announcements/announcing-audio-apis
89. https://openrouter.ai/docs/guides/overview/multimodal/stt.md
90. https://openrouter.ai/docs/cookbook/audio/two-voice-podcast.md
91. https://openrouter.ai/docs/guides/overview/multimodal/audio.md
92. https://www.cs.rochester.edu/hci/pubs/pdfs/edX-MOOC-video-production-and-engagement_LAS-2014.pdf
93. https://digitalcommons.memphis.edu/facpubs/8941
94. https://perticles.blogspot.com/2008/01/muller-sharma-reimann-sci-ed-2008.html
95. https://perticles.blogspot.com/2008/01/muller-sharma-reimann-jcal-2008.html
96. https://api.crossref.org/works/10.1080/03640210701863396
97. https://lucidea.com/blog/mayers-12-principles-of-multimedia-learning
98. https://github.com/souzatharsis/podcastfy/blob/main/usage/conversation_custom.md
99. https://huggingface.co/hexgrad/Kokoro-82M
100. https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md
101. https://github.com/hexgrad/kokoro/blob/main/kokoro/pipeline.py
102. https://pypi.org/project/kokoro/
103. https://pypi.org/project/kokoro-onnx/
104. https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1
105. https://github.com/OHF-Voice/piper1-gpl
106. https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/ALIGNMENTS.md
107. https://github.com/rhasspy/piper
108. https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/libritts_r/medium/MODEL_CARD
109. https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html
110. https://huggingface.co/coqui/XTTS-v2/blob/main/LICENSE.txt
111. https://pypi.org/project/edge-tts/
112. https://learn.microsoft.com/en-us/answers/questions/2088770/are-opensource-edge-tts-free-for-commercial-use
113. https://developers.openai.com/api/docs/guides/text-to-speech
114. https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps
115. https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert-with-timestamps
116. https://elevenlabs.io/docs/help-center/legal/can-i-publish-the-content-i-generate-on-the-platform
117. https://huggingface.co/Supertone/supertonic-3
118. https://reddit.sentinel-team.org/posts/1tgnxnm/snapshots/2026-05-23T00%3A36%3A34.219378Z
119. https://github.com/remsky/Kokoro-FastAPI
120. https://github.com/m-bain/whisperX
121. https://github.com/jianfch/stable-ts
122. https://github.com/readbeyond/aeneas
123. https://docs.astral.sh/uv/guides/integration/pytorch/
124. https://pypi.org/project/onnxruntime/
125. https://ffmpeg.org/ffmpeg-filters.html
126. https://ffmpeg.org/ffmpeg-formats.html
127. https://academysoftwarefoundation.github.io/EncodingGuidelines/Encodeh264.html
128. https://podcasters.apple.com/support/893-audio-requirements
129. https://www.gnu.org/licenses/gpl-faq.html#WhatCaseIsOutputGPL
130. https://pypi.org/project/ffmpeg-normalize/
131. https://pypi.org/project/pyloudnorm/
132. https://huggingface.co/spaces/prathje/gradio_motioncanvas_agent/blob/af6ade43b266650ec8f4543ee281c3d356b26731/README.md

Local files (read in this session):
- L1. /home/user/skyweave/instructional_video/ivideo/openrouter.py
- L2. /home/user/skyweave/instructional_video/ivideo/assemble.py
- L3. /home/user/skyweave/instructional_video/setup.sh
- L4. /home/user/skyweave/instructional_video/README.md
