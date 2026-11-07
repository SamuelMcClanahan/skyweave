# DRONE STACK — PHASE E1 BRIEF (pre-writable software)
Date: 2026-10-08. Ratified by Samuel in the spec discussion of this date.
Audience: a coding agent, local or a Claude Code cloud session on
github.com/SamuelMcClanahan/skyweave.
Discipline: this file is the hand-off boundary. Work only what it sanctions.
Labels: Measured / Chosen / Provisional keep their usual meaning. Nothing in
this phase promotes a number to Measured.

## 0. Ground rules
- Verify repo facts yourself (paths, test patterns). Do not trust summaries.
- All new code targets the contracts in section 4, never an old module's
  internals.
- The percepd v0 and tracker v0 SOURCE is missing from the repo (recovery in
  progress; deployed artifacts exist on the Cubie board). Build against the
  contracts and the harness's synthetic detection source. Work item 7
  (percepd v1) is BLOCKED; skip it and say so.
- SITL only. No code path in this phase may arm or command a real FC. The
  setpoint gate (work item 3) ships disabled by default.
- Work on a branch; open PRs; never push main directly.
- Stop and report on any surprise that would need a contract change.

## 1. What the system is now
A 5-inch quad (pusher, symmetric X) with a Radxa Cubie A7S companion and an
AR0234 camera finds, tracks, and approaches an owned soft target (tethered
balloon first, then kite, thrown paper airplane last). Controlled, short,
human-managed demos. A human primes the drone on the ground with the trial
parameters; after priming it launches, searches, and acquires on its own.
Engaging needs human authorization (live, or given at prime time). The human
can mark COMPLETE and can always abort. Ground cueing from SkyWeave is
DROPPED for now (returns later for full autonomy). The FC (Matek H743 SLIM
V4, ArduPilot Copter 4.7.0) owns stabilization, arming, and failsafe. The
companion never generates motor output and only sends velocity setpoints in
GUIDED.

## 2. Part A — frozen facts (do not relitigate)
- Companion: Cubie A7S. NPU works only on the Rabs9 kernel (boot entry l2).
  Measured inference: yolo26n 11.9 ms, yolo11n 12.1 ms, ResNet50 7.3 ms.
  Flight clock 852 MHz.
- Camera: AR0234 USB3 UVC. Flight capture YUYV 1920x1200@80. MJPG rejected.
  640x360@120 reserved sprint mode.
- percepd v0 (Measured): 18.4 ms glass-to-boxes @54 FPS sequential; 27.3 ms
  @60 FPS pipelined keep-latest.
- Tracker v0 (spec on record): per-axis 2-state KF [pos, vel], constant
  velocity; IoU association; M-of-N confirm; coasting with a miss cap.
  1.4 px RMS, zero ghosts on 5 synthetic tests.
- FC: SERIAL1 (UART7) MAVLink2 @921600 for the companion. 1251-param
  baseline versioned. SITL-proven command path: GUIDED arm, takeoff,
  velocity setpoint (velocity-only mask, resent >=2 Hz, achieved 1.99 of
  2.00 m/s), land.
- Vehicle decisions on record: pusher configuration (prop-free nose, camera
  aligned with flight direction), symmetric X, global shutter chosen for
  terminal guidance, ELRS receiver, u-blox NEO-M9N GPS, AM32 55A ESC.

## 3. Decisions closed 2026-10-08
3.1 Autonomy scope: self-search and self-acquire, no ground cue. The
    working document's no-open-world-search line is amended (section 8).
3.2 Lock stickiness: guidance holds exactly one engaged_track_id and
    consumes only that track. The id changes ONLY by explicit event: human
    command, or engaged-track death. No code path may retarget because
    another track scores higher.
3.3 Mission state machine:
    PRIMED -> LAUNCH -> SEARCH -> ACQUIRING -(human approval)-> ENGAGED
    <-> COASTING; ENGAGED -> TOUCH (touch trials) -> COMPLETE or MISS;
    COASTING -> LOST past the coast cap; LOST -> SEARCH (bounded reacquire
    window) or RETURN on budget; COMPLETE / ABORT / budget / link loss ->
    RETURN -> LAND. ABORT reachable from every state, highest precedence.
    Every exit path is an FC-owned mode change (RTL / LAND). Wi-Fi abort is
    a soft abort (request). The RC mode switch is the hard abort and works
    with the companion dead.
3.4 Terminal TOUCH state (soft-contact demos): owned soft unmanned targets
    only; controlled site, no people in the operating volume; nose-first
    contact, never props; closure speed capped. Commit gate is image-space:
    commit when box fill w/1920 >= alpha AND center offset <= beta, with
    the track confirmed (k consecutive hits, not coasting). After commit:
    open-loop velocity through the predicted contact point, then brake and
    climb. v1 flight profile: ONE pass, unconditional RTL; hit or miss is
    decided on the ground by the human plus replay. Autonomous MISS
    handling and retry (look-back reacquire, balloon only) are v2: specced,
    not flown. Speed rises only after two clean trials at the current cap,
    never in the same trial as a gains change.
3.5 Range: apparent-size range Z = fW/w (known target width W) primary;
    assume-and-bound fallback. Onboard scope is bearing plus size-range
    only; no onboard 3D target estimation (B6 closed).
3.6 Guidance v1: de-rotate the track with FC attitude; pure pursuit
    (velocity along the line of sight toward the standoff point); P
    controller with velocity clamp, accel rate limit, deadband. Pluggable
    law behind a stub interface; the harness ranks candidate laws.
3.7 Contracts: JSON v1 FROZEN (section 4). Deferred with triggers:
    binary packets (trigger: measured serialization cost), Mahalanobis
    association (trigger: recorded IoU failure in replay), IMM (trigger:
    recorded CV-model failure on a real maneuver).
3.8 Recording (B5): packets only, whole flight — detections, tracks,
    mission state, fc_link health, full MAVLink log. NO in-flight frame
    recording for now. Training data comes from ground sessions. The miss
    vector is computed from track and state packets at the commit event:
    miss ~= Z * eps / f, with eps the pixel offset at commit.
3.9 Detector plan (B4, context only this phase): custom single-class
    ("target") nano model; AR0234 footage auto-labeled by classical CV plus
    synthetic renders; negatives (birds, planes, clouds) included; convert
    through the existing NBG pipeline; replay gate on held-out real clips
    before flight. Cue/classifier branch stays a hook with policy = never.
    Demo geometry keeps the target above the drone's horizon.
3.10 Launch: ground auto-takeoff from a flat stand (non-ferrous, heading
    index, level reference). Gimbal launcher REJECTED: a quad self-aims in
    flight. Hand-launch / ArduPilot THROW mode shelved, hazard on record
    (prop spin-up near hands). For the thrown-plane demo the drone hovers
    first and the thrower throws through its view.
3.11 Radio (config, not code): one 3-position switch = MANUAL / GUIDED /
    RTL (engage gate and hard abort). A guarded momentary (EdgeTX logical
    switch, hold >= 0.5 s) = motor emergency stop, last resort. A spare aux
    channel = approve input, read by the companion via MAVLink RC
    passthrough. EdgeTX voice or haptic per switch position.

## 4. Contracts to write and freeze (work item 1)
Home: v2/docs/DRONE_CONTRACTS_D0.md, fenced like DETECTION_CONTRACTS_D0.md.
Golden packet fixtures beside the tests. All packets UDP JSON with integer
field "v" (start 1); receivers ignore unknown fields; additive-only
changes; a breaking change bumps v.
- detection packet (percepd -> tracker), per v0 semantics: v, t_cap
  (board-monotonic ms), frame_seq, boxes [{x, y, w, h, conf}].
- track packet (tracker -> guidance): v, t_cap, track_id, state
  (tentative | confirmed | coasting), u, v_px, du, dv (px/s), box w, h,
  hits, misses, age_frames.
- mission state packet (guidance out, logged and shown on the UI): v, t,
  mission_state (PRIMED, LAUNCH, SEARCH, ACQUIRING, ENGAGED, COASTING,
  TOUCH, COMPLETE, MISS, LOST, RETURN, LAND, ABORT), engaged_track_id
  (nullable), trial parameter echo {trial_type, v_max, alpha, beta, k,
  pass_budget, search_alt, d_s}, events [{t, name}] for transitions,
  commit, pass done, tones.
- fc_link health packet: v, t, attitude_age_ms, fc_link_up, rc_seen,
  gate_state (locked | enabled), last_setpoint_t.
- command packet (UI or radio -> mission): v, cmd_id, token, command
  (prime{params} | approve_engage | mark_complete | abort); ack {cmd_id,
  result}. Each cmd_id executes at most once. A command invalid in the
  current state is rejected and logged, never silently dropped.

## 5. Work items and acceptance gates
1. Contracts doc + golden fixtures. Gate: doc in place, fixtures with
   round-trip encode/decode tests, suite green.
2. Mission state machine. Pure logic module plus adapters; a who-may-
   trigger table for every transition (tracker event, human, budget,
   failsafe). Gate: unit tests cover every transition and every rejection;
   a recorded packet stream replays deterministically to the same states.
3. fc_link v1. MAVLink2 to SITL (serial later): attitude subscription at
   50 Hz with timestamping; staleness tracking (degraded flag past the
   bound); health packet at ~1 Hz; gated velocity setpoint sender with
   clamps, disabled by default; RC channel passthrough read (approve
   switch); PLAY_TUNE helper for state tones. Gate: scripted SITL run
   (arm, takeoff, velocity, land) passes; injected attitude staleness
   trips the degraded path; sends are provably blocked while the gate is
   disabled.
4. Guidance v1. De-rotation, size-range, pure pursuit to standoff, clamps
   and deadband, image-space commit gate, open-loop terminal segment plus
   brake-and-climb, miss-vector computation logged at commit. Gate: unit
   tests on synthetic geometry (known offsets produce known commands and
   known miss vectors); passes the harness scenarios.
5. Closed-loop SITL harness. Synthetic target trajectories (static
   balloon, slow kite, ballistic thrown plane); camera projection to
   detection packets with noise, dropout, and injected boresight error;
   tracker; guidance; state machine; SITL. If tracker v0 source is not
   recovered in time, reimplement it from the section 2 spec and label it
   Provisional (its numbers must be re-earned). Scripted scenarios:
   nominal standoff hold, touch pass, detection dropout, link loss at
   commit, late abort, target maneuver. Scorecard JSON per run: p95 miss
   at the commit plane, standoff hold error, lock retention, abort
   latency, safety floors (no geofence breach). Gate: all scenarios green
   under fixed seeds, scorecard emitted.
6. Ground UI. Served by the companion process (a laptop browser is the
   client): state banner, engaged track, battery, link health; exactly
   four commands (prime with the parameter form, approve engage, mark
   complete, abort) with ack-by-id and a shared-secret token; no joystick,
   ever. Gate: headless tests prove command idempotency and invalid-state
   rejection; the page renders from a recorded packet stream.
7. percepd v1 — BLOCKED on source recovery. For later: systemd unit;
   config file; kernel preflight (refuse loudly off the Rabs9 kernel);
   heartbeat ~1 Hz; model swap by config (NBG path); "v" field per the
   contract; pipeline mode default sequential (config switch); cue hook
   present, policy = never.
8. Doc hygiene (section 8).

## 6. Provisional defaults (prime-time parameters; tune in the harness)
- closure speed cap v_max: 2.5 m/s (touch trials)
- standoff distance d_s: 5 m; COMPLETE (standoff trials): hold within
  +/- 1 m for T = 10 s
- commit fill alpha: 0.4; centering beta: 0.1 of frame width; k = 5
  consecutive hits to allow commit
- tracker: M = 3 of N = 5 confirm; coast cap 20 frames
- pass budget: 1 (v1)
- search: yaw scan 45 deg/s at the primed search altitude; reacquire
  window after LOST: 10 s
- fc_link: attitude 50 Hz; staleness bound 100 ms; setpoint loop 10 Hz
- budgets at prime: flight time cap, battery floor, geofence radius

## 7. Fences
- Fenced paths per repo rules (v1/, golden/, proto/, contracts other than
  the new doc this brief sanctions).
- No gate or acceptance scenes as inputs to anything.
- No in-flight frame recording code paths.
- No real-FC sends; no FC parameter changes; SITL only.
- Do not edit SKYWEAVE_DRONE_WORKING_DOCUMENT.md beyond section 8's list.
- Evidence and session material never enters the repo; the session-artifact
  .gitignore rules stay in force.

## 8. Doc hygiene (mechanical, same branch)
- Rename A7Z to A7S everywhere it denotes the build board.
- Working doc section 3: amend the safety boundary with the soft-contact
  terminal state and its conditions (3.4 above), dated 2026-10-08.
- Section 14 ledger entries, dated 2026-10-08: ground cueing dropped;
  onboard search adopted; TOUCH state reviewed and adopted; gimbal
  launcher rejected; throw mode shelved with hazard; JSON v1 frozen;
  packets-only recording; pusher and symmetric X folded in.
- Section 15: mark Q4, Q5, Q18 answered; add: boresight calibration from
  logged miss vectors; dataset and negatives plan; Cubie Wi-Fi AP range at
  the field.
- Note where missing drone docs (DRONE_STACK_SPEC_HANDOFF.md and the D8
  set) should be restored from: Samuel holds copies in his Cowork Project
  and supplies the files; do not reconstruct them from memory.

## 9. Hands and admin (Samuel, informational — not agent work)
Battery purchase and integration; UART solder and fc_link on the real FC;
accel cal; power draw; 30-min thermal soak; thrust stand; weigh the build;
arm coupons; manual flight with the Cubie powered and logging BEFORE any
autonomy flies; launch stand build (non-ferrous, heading index); radio
switch configuration per 3.11; percepd/tracker source recovery; flying
site and airspace check.

## 10. Hand-back checklist
- One branch (or one per work item), PRs opened, nothing pushed to main.
- Full suite green; new tests included; gitleaks clean on the diff.
- Harness scorecard JSON attached to the hand-back note.
- A short SHIFT-style summary: what was built, dead ends with reasons,
  the three most useful next experiments for the guidance tuning.
- Adversarial self-review done before hand-back, findings listed.
