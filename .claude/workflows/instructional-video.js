export const meta = {
  name: 'instructional-video',
  description: 'Make an animated instructional video: OpenRouter writes the dialogue, Kokoro speaks it, Claude Opus agents animate each scene in Manim with a render-look-fix loop and an independent review, ffmpeg assembles',
  whenToUse: 'Run with args {topic, minutes?, audience?, speakers?, project?, sources?, model?} to produce instructional_video/projects/<project>/out/final.mp4. Re-run with the same project to resume: approved scenes are kept.',
  phases: [
    { title: 'Prepare', detail: 'setup check, OpenRouter dialogue, Kokoro voice, timing' },
    { title: 'Animate', detail: 'one Claude Opus animator per scene: write Manim, render, look at frames, fix', model: 'opus' },
    { title: 'Review', detail: 'independent Claude Opus reviewer per scene, repair loop', model: 'opus' },
    { title: 'Assemble', detail: '1080p renders, mux narration, subtitles, loudness' },
    { title: 'Final check', detail: 'Claude Opus checks the finished video', model: 'opus' },
  ],
}

// ---------- inputs ----------
const A = args && typeof args === 'object' ? args : (typeof args === 'string' ? { topic: args } : {})
if (!A.topic && !A.script_file && !A.project) {
  throw new Error('Pass args like {"topic": "How a Kalman filter works", "minutes": 3}. Or {"project": "<slug>"} to resume, or {"script_file": "path/to/script.json"} to skip OpenRouter.')
}
const MAX_FIX_ROUNDS = Number.isInteger(A.max_fix_rounds) ? A.max_fix_rounds : 2
const MAX_RENDERS = Number.isInteger(A.max_renders) ? A.max_renders : 6
const ANIM_MODEL = A.animator_model || 'opus'

const TOOL = `The pipeline is the Python package in the folder instructional_video/ at the top of the git repository (find it with: git rev-parse --show-toplevel). Run every command from that folder with "uv run ivideo ...". Every ivideo command prints JSON.`

// ---------- schemas ----------
const LINE = {
  type: 'object',
  properties: {
    index: { type: 'integer' }, speaker: { type: 'string' }, text: { type: 'string' },
    visual: { type: 'string' }, start: { type: 'number' }, end: { type: 'number' },
  },
  required: ['index', 'speaker', 'text', 'visual', 'start', 'end'],
}
const PREP_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    error: { type: 'string' },
    project: { type: 'string', description: 'project slug (folder name under instructional_video/projects/)' },
    project_dir: { type: 'string', description: 'absolute path' },
    latex: { type: 'boolean' },
    dialogue_source: { type: 'string', description: 'openrouter:<model id> | existing script.json | script_file' },
    video_title: { type: 'string' },
    total_duration: { type: 'number' },
    speakers: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, name: { type: 'string' }, role: { type: 'string' } }, required: ['id', 'name', 'role'] } },
    scenes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' }, title: { type: 'string' }, learning_goal: { type: 'string' },
          visual_plan: { type: 'string' }, duration: { type: 'number' },
          state: { type: 'string', description: 'from ivideo status: todo|written|rendered|approved|failed' },
          lines: { type: 'array', items: LINE },
        },
        required: ['id', 'title', 'learning_goal', 'visual_plan', 'duration', 'state', 'lines'],
      },
    },
  },
  required: ['ok', 'project', 'project_dir', 'latex', 'scenes'],
}
const ANIM_SCHEMA = {
  type: 'object',
  properties: {
    scene: { type: 'string' },
    ok: { type: 'boolean', description: 'true if the last draft render succeeded' },
    renders: { type: 'integer' },
    clean: { type: 'boolean', description: 'report.clean of the last render' },
    remaining_issues: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string', description: 'deviations from the script visuals and why' },
  },
  required: ['scene', 'ok', 'renders', 'clean', 'remaining_issues'],
}
const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    scene: { type: 'string' },
    pass: { type: 'boolean', description: 'true only if there is no blocker or major issue' },
    score: { type: 'integer', description: '1-10 overall quality as teaching animation' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          line_index: { type: 'integer', description: '-1 for whole scene' },
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          problem: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['line_index', 'severity', 'problem', 'fix'],
      },
    },
  },
  required: ['scene', 'pass', 'score', 'issues'],
}
const ASSEMBLE_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' }, error: { type: 'string' },
    final: { type: 'string' }, subtitles: { type: 'string' }, contact_sheet: { type: 'string' },
    duration: { type: 'number' }, expected_duration: { type: 'number' },
    streams: { type: 'array', items: { type: 'string' } },
    failed_scenes: { type: 'array', items: { type: 'string' } },
  },
  required: ['ok'],
}
const FINAL_SCHEMA = {
  type: 'object',
  properties: {
    pass: { type: 'boolean' },
    audio_ok: { type: 'boolean' },
    duration_ok: { type: 'boolean' },
    consistency_ok: { type: 'boolean' },
    issues: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
  required: ['pass', 'issues', 'summary'],
}

// ---------- Prepare ----------
phase('Prepare')
const prep = await agent(`${TOOL}

You prepare an instructional video project. Inputs (JSON): ${JSON.stringify(A)}

Steps:
1. Run "uv run ivideo doctor". If ready_for_render is false, run "./setup.sh" (add "--latex" if args.latex is true) and run doctor again. If it still fails, stop and return ok=false with the error.
2. Choose the project slug: args.project if given; otherwise a short lowercase-hyphen slug of args.topic (max 48 chars). The project folder is projects/<slug>/.
3. Dialogue. The spoken dialogue MUST come from OpenRouter (or from a script file the user gave). You must NOT write, rewrite, or edit any dialogue text yourself.
   - If args.script_file is set: copy that file to projects/<slug>/script.json.
   - Else if projects/<slug>/script.json exists and args.redo_dialogue is not true: keep it.
   - Else: run "uv run ivideo new <slug> --topic ..." with --audience, --minutes, --speakers, --notes and one --source per path in args.sources when given (quote arguments correctly). Then run "uv run ivideo dialogue <slug>" with --model args.model and one --fallback per args.fallbacks entry when given. If OPENROUTER_API_KEY is missing or the call fails, stop and return ok=false with the exact error.
   - If you made a NEW script.json in a folder that had an old run, delete only these old outputs inside projects/<slug>/: status.json, timing.json, audio/, scenes/, renders/, frames/, out/.
4. Voice: if timing.json is missing or older than script.json, run "uv run ivideo voice <slug>" (add --speed args.speed if given, and one --voice id=voice per entry of args.voices if given).
5. Run "uv run ivideo brief <slug>" and "uv run ivideo status <slug>" and "uv run ivideo doctor".
Return: project slug, absolute project_dir, latex (from doctor), dialogue_source, video_title, total_duration, speakers, and every scene with its id, title, learning_goal, visual_plan, duration, state (from status) and all lines with index, speaker, text, visual, start, end (copied exactly from brief).`, { label: 'prepare', phase: 'Prepare', schema: PREP_SCHEMA, effort: 'low' })

if (!prep || !prep.ok) {
  return { ok: false, stage: 'prepare', error: prep ? prep.error : 'prepare agent returned nothing' }
}
log(`${prep.video_title || prep.project}: ${prep.scenes.length} scenes, ${Math.round(prep.total_duration || 0)} s of narration (${prep.dialogue_source || 'dialogue'})`)

const P = prep.project
const PDIR = prep.project_dir
const outline = prep.scenes.map(s => `${s.id}: ${s.title} (goal: ${s.learning_goal})`).join('\n')
const speakers = (prep.speakers || []).map(s => `${s.id} = ${s.name}, ${s.role}`).join('; ')
const redo = new Set(Array.isArray(A.redo_scenes) ? A.redo_scenes : [])
const todo = prep.scenes.filter(s => s.state !== 'approved' || redo.has(s.id))
const kept = prep.scenes.filter(s => !todo.includes(s))
if (kept.length) log(`Keeping approved scenes: ${kept.map(s => s.id).join(', ')}`)

const sceneBlock = s => `Scene ${s.id} "${s.title}". Learning goal: ${s.learning_goal}
Overall visual plan: ${s.visual_plan}
Scene duration: ${s.duration.toFixed(2)} s (the video must end exactly then; finish() does this).
Lines (times are seconds from scene start; the narration audio is fixed):
${s.lines.map(l => `  [${l.index}] ${l.start.toFixed(2)}-${l.end.toFixed(2)}s  ${l.speaker}: "${l.text}"\n       visual: ${l.visual}`).join('\n')}`

const API = `Scene file contract (Manim Community 0.22):
  from ivideo.synced import *          # gives all of manim, the style helpers, and SyncedScene
  class Main(SyncedScene):
      def construct(self):
          ...
          self.finish()                    # MUST be the last call
SyncedScene API:
  self.lines[i]            .start .end .duration .mid .text .visual .speaker
  self.at_line(i, offset=0)  wait until line i starts (+offset s). Call it before you animate line i's visual.
  self.fit(i, fraction=0.5, lo=0.3, hi=2.5)  a run_time that uses a fraction of the time left in line i.
  self.remaining(i)        seconds left in line i.   self.wait_until(t)   wait to absolute scene time t.
  self.scene_title, self.scene_duration, self.now
  Background color is already set. Do not set config, resolution, or add_sound. Do not call self.wait() with long fixed times; use at_line/finish.
Style helpers (use them so all scenes match): title(str) -> top-left title with underline; text(str, size=32, color=FG); label(str) (26 pt);
  colors BG FG MUTED ACCENT(blue) ACCENT_2(amber) GOOD(green) BAD(red) PURPLE; sizes TITLE_SIZE 40, BODY_SIZE 32, LABEL_SIZE 26, MIN_FONT_SIZE 20;
  safe_box() -> (left, right, bottom, top); content_top(); fit_width(mob). FONT = "DejaVu Sans".
LaTeX is ${prep.latex ? 'installed: MathTex/Tex are allowed for formulas' : 'NOT installed: do not use MathTex, Tex, or anything that needs LaTeX; write formulas with Text'}.`

const RULES = `Layout and teaching rules:
- Start with title(self.scene_title) at the top-left during line 0; keep it unless the scene needs the space.
- Each line gets a visible change that matches its words (appear, move, highlight, transform). Start it at at_line(i); finish it inside the line.
- Keep at most about 6 visible elements. Fade out or move what the narration no longer talks about.
- On-screen text: short labels (at most about 8 words), font size >= 24, never paragraphs, never the full narration.
- Everything stays inside safe_box(). Use arrange/next_to with buff >= 0.25. Labels never sit on top of other text or lines; offset them or add a background (BackgroundRectangle / add_background_rectangle).
- Same concept = same color in every scene. Diagrams over decoration. Prefer simple shapes (Circle, Square, Arrow, Line, Dot, Axes, NumberLine, Rectangle, Polygon).
- Motion is calm: run_times about 0.5-1.5 s; nothing flashes.
- Accuracy: any number, term, or claim on screen must agree with the narration.`

const LOOP = (sid, maxRenders) => `Work loop (at most ${maxRenders} renders):
1. Run "uv run ivideo render ${P} ${sid}" (480p draft). Read the JSON.
   - ok=false: read log_tail (and the .log file if needed), fix the code, render again.
   - layout_issues / drift / duration_error not ~0: fix them (drift means animations inside a line took longer than the line).
2. LOOK at the frames: use the Read tool on frames.contact_sheet and on each frames/*.png it lists. Check each line's frame against its visual and words: right content, readable, nothing overlapping, nothing cut off, not empty, clear what to look at.
3. Fix and render again until the report is clean and the frames look right.`

// ---------- Animate + Review (pipeline per scene) ----------
const results = await pipeline(
  todo,
  s => agent(`${TOOL}

You are the animator for one scene of an instructional video "${prep.video_title}". Speakers: ${speakers}.
Write ${PDIR}/scenes/${s.id}.py. The narration audio already exists and is fixed; your animation must match it.

Whole video outline (other scenes are animated by other agents at the same time; stay consistent and do not repeat their content):
${outline}

${sceneBlock(s)}

${API}

${RULES}

${LOOP(s.id, MAX_RENDERS)}

If the scene file already exists from an earlier run, start from it and improve it.
When done, run "uv run ivideo mark ${P} ${s.id} written --note '<one line>'". Return the result.`, { label: `animate:${s.id}`, phase: 'Animate', schema: ANIM_SCHEMA, model: ANIM_MODEL }),

  async (anim, s) => {
    const rounds = []
    const review = () => agent(`${TOOL}

You are an independent reviewer of one animated scene of an instructional video "${prep.video_title}". You did not make it. Do NOT edit any file except through "uv run ivideo mark".
Be strict: a viewer must understand each line from the screen plus the voice.

${sceneBlock(s)}

Steps:
1. Run "uv run ivideo frames ${P} ${s.id}" and read ${PDIR}/renders/draft/${s.id}.report.json.
2. Use the Read tool to look at the contact sheet and at EVERY frame PNG it lists (one per line, at 70% through the line, plus the last frame).
3. For each line judge: does the screen show what the words talk about (the visual field is the plan; small deviations are fine if clear)? Is all text readable (size, contrast) and spelled right? Any overlap, cut-off, clutter, or empty screen? Any on-screen claim that disagrees with the narration? Is there a visible change between consecutive lines?
4. Severity: blocker = broken, unreadable, wrong facts, or nothing matches the words; major = overlap, cut-off text, clutter, a line with no matching visual, timing clearly off; minor = polish.
5. Set "pass" to true only if there is no blocker and no major issue. If pass, run "uv run ivideo mark ${P} ${s.id} approved --note 'score N'".
Give each issue a concrete fix an animator can apply (what object, where, what change).`, { label: `review:${s.id}`, phase: 'Review', schema: REVIEW_SCHEMA, model: ANIM_MODEL })

    let rv = anim && anim.ok ? await review() : null
    let round = 0
    while (round < MAX_FIX_ROUNDS && (!rv || !rv.pass)) {
      round++
      const issues = rv ? rv.issues.filter(i => i.severity !== 'minor').concat(rv.issues.filter(i => i.severity === 'minor')) : []
      const fixPrompt = rv
        ? `An independent reviewer rejected the scene. Fix every blocker and major issue; fix minor ones if cheap:\n${issues.map(i => `- [${i.severity}] line ${i.line_index}: ${i.problem} -> fix: ${i.fix}`).join('\n')}`
        : `The previous animator did not finish with a working render${anim ? `: ${JSON.stringify(anim.remaining_issues)}` : ''}. Make the scene render and look right.`
      const fixed = await agent(`${TOOL}

You repair one animated scene of an instructional video "${prep.video_title}". Edit ${PDIR}/scenes/${s.id}.py.

${fixPrompt}

${sceneBlock(s)}

${API}

${RULES}

${LOOP(s.id, Math.max(3, Math.floor(MAX_RENDERS / 2)))}

When done, run "uv run ivideo mark ${P} ${s.id} written --note 'repair round ${round}'". Return the result.`, { label: `repair:${s.id}#${round}`, phase: 'Review', schema: ANIM_SCHEMA, model: ANIM_MODEL })
      rounds.push({ round, review: rv, repair: fixed })
      rv = fixed && fixed.ok ? await review() : rv
    }
    return { scene: s.id, title: s.title, animate: anim, review: rv, repair_rounds: round, approved: !!(rv && rv.pass), history: rounds }
  },
)

const sceneResults = results.map((r, i) => r || { scene: todo[i].id, approved: false, error: 'agent failed' })
const notApproved = sceneResults.filter(r => !r.approved).map(r => r.scene)
if (notApproved.length) log(`Not approved after ${MAX_FIX_ROUNDS} repair rounds: ${notApproved.join(', ')} (assembled anyway; see results)`)

// ---------- Assemble ----------
phase('Assemble')
const asm = await agent(`${TOOL}

Assemble the finished video for project ${P}.
1. Run "uv run ivideo render-all ${P} --quality final" (1080p30; can take several minutes). If a scene fails at final quality but worked as draft, read its log in ${PDIR}/renders/final/, make the smallest fix in ${PDIR}/scenes/<id>.py that does not change how it looks, and render that scene again with "uv run ivideo render ${P} <id> --quality final --no-frames".
2. Run "uv run ivideo assemble ${P}"${A.burn_subtitles ? ' --burn-subtitles' : ''}.
3. Return the assemble JSON fields (final, subtitles, contact_sheet, duration, expected_duration, streams) and ok. List scenes that could not be rendered in failed_scenes.`, { label: 'assemble', phase: 'Assemble', schema: ASSEMBLE_SCHEMA, effort: 'low' })

if (!asm || !asm.ok) {
  return { ok: false, stage: 'assemble', project_dir: PDIR, error: asm ? asm.error : 'assemble agent returned nothing', scenes: sceneResults }
}

// ---------- Final check ----------
phase('Final check')
const fin = await agent(`${TOOL}

Check the finished instructional video ${asm.final} (project ${P}). Do not edit files.
1. Read ${PDIR}/out/assembly.json. Duration must be within 0.5 s of expected_duration; streams must include h264 video, aac audio, and mov_text subtitles (unless subtitles were burned in).
2. Audio: run ffmpeg -i ${asm.final} -af volumedetect -vn -f null - and confirm mean_volume is not silent (above -40 dB).
3. Use the Read tool on ${asm.contact_sheet} and on 3 more frames you grab at scene boundaries (ffmpeg -ss <t> -i ${asm.final} -frames:v 1 /tmp/x.png; offsets are in assembly.json). Check that the scenes look like one video: same background, title placement, fonts, and colors; nothing broken at 1080p.
4. Read ${PDIR}/out/final.srt briefly: line order and speaker names make sense.
Return pass, audio_ok, duration_ok, consistency_ok, issues, and a 3-sentence summary.`, { label: 'final-check', phase: 'Final check', schema: FINAL_SCHEMA, model: ANIM_MODEL })

return {
  ok: true,
  project: P,
  project_dir: PDIR,
  final: asm.final,
  subtitles: asm.subtitles,
  contact_sheet: asm.contact_sheet,
  duration: asm.duration,
  dialogue_source: prep.dialogue_source,
  scenes_kept: kept.map(s => s.id),
  scenes: sceneResults.map(r => ({ scene: r.scene, approved: r.approved, score: r.review ? r.review.score : null, repair_rounds: r.repair_rounds, open_issues: r.review ? r.review.issues : [] })),
  final_check: fin,
}
