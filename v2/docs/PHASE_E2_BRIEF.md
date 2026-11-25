# DRONE STACK — PHASE E2 BRIEF (balloon intercept)
Date: 2026-11-21. Decisions taken by Samuel with Claude on this date, while
working through the E2 blockers. They are recorded in
`DRONE_CONTRACTS_D0.md` §11.2 (`E2-` IDs), next to the 2026-11-20 vetting
decisions in §11.1 (`V-` IDs).
Status: DRAFT until Samuel ratifies it. Part E2a (section 4) is the work this
brief sanctions. Part E2b (section 5) is an outline. It is not ready: its
brief is finalized at the E2a hand-back.
Audience: a coding agent, local or a Claude Code cloud session on
github.com/SamuelMcClanahan/skyweave.
Discipline: this file is the hand-off boundary. Work only what it sanctions.
Labels: Measured / Chosen / Provisional keep their usual meaning. Nothing in
this phase promotes a number to Measured.

## 0. Ground rules
- Everything from the E1 brief §0 still holds: verify repo facts yourself;
  code targets the contract, never another module's internals; SITL only;
  branch and PR, never push main; stop and report on any surprise that needs
  a contract change.
- A contract clause changes only in a PR that cites the decision ID that
  authorizes it (`V-` in §11.1, `E2-` in §11.2).
- Run tests and SITL through `v2/docker/sitl/run.sh`, not `uv` on the Mac.
  Run outputs go under `<repo>/_local/sim-runs/...` and never enter the repo.
- **Never add a parameter-write path to fc_link** ([F1], V-D7).
- Tune on probe seeds only. Never on gate seeds or gate scenes.
- The E1 gate (S1-S6, contract §8) stays green, unchanged, on every E2 PR
  that touches guidance, the tracker, the camera model, or the harness
  (E2-D1).

## 1. Goal
Intercept a balloon. The vehicle searches from below, engages a balloon,
climbs into it, and touches it nose first. The closing pass uses
proportional navigation (PN). Only touch trials are in scope. Standoff
trials, kites, and thrown planes are out of E2 (E2-D7).

The vehicle is the cone airframe (V-F1, V-F6): an X quad with a tall tower,
the camera at the nose tip, the nose along the thrust axis. For now the
camera looks straight up along the nose axis with a 100 deg lens (E2-D2). A
level vehicle sees only targets at least 40 deg above its horizon, so the
operator flies the search below the balloon (E2-D6).

## 2. Read first
- `DRONE_CONTRACTS_D0.md` §11.1 and §11.2: the decisions this phase builds.
- `DRONE_CONTRACTS_D0.md` §8.1 ([S9]-[S12]): probe scenarios, wind, bad
  frames, the cone frame file.
- The probe baseline in PR SamuelMcClanahan/skyweave#3 (today's guidance and
  tracker on P1-P7, with findings V-S1 to V-S12). P3 and P4 show the
  tracker breaking under the vehicle's own yaw and on fast boxes (V-S8).
- `TESTING_DOCTRINE.md`: tests cite clauses, use fixtures over mocks, assert
  against the system's own outputs, and keep the fast tier fast.

## 3. Decisions and where they land

| Decision | What | Part |
| --- | --- | --- |
| V-D1, E2-D2 | Camera mount and lens from config; `cone_up` mount, 100 deg lens | E2a |
| V-D11 | Lens distortion model, in the camera model and in `camera_sim` | E2a |
| E2-D5 | The tracker runs inside the companion process | E2a |
| V-D10 | The tracker filters world angles, not pixels | E2a |
| V-D9 | Width rate on the track packet; time to contact | E2a |
| E2-D1 | E2 gate frozen before tuning; E1 gate kept | E2b (freeze), all (E1 gate) |
| E2-D6 | Search from below; probes and gate put targets 40-80 deg above | E2b |
| V-D3, V-D4 | Hold point about 2 m from the target, below it; commit gate retuned | E2b |
| V-D2, V-D5, V-D6, V-D12 | PN pass as acceleration setpoints at 50 Hz; steer until the hand-off; dropout and death in the pass | E2b |
| V-D7 | FC parameters the operator sets; in SITL, through the defaults file only | E2b |
| E2-D4 | The E2 gate does not check the `v_max` floor; it reports peak speeds | E2b |
| E2-F1 | ArduCopter 4.7.0 acceleration setpoints, from a source read; prove on SITL first | E2b |

## 4. Part E2a — camera and tracker geometry (sanctioned)
E2a changes how the system sees. It does not change how it flies: guidance
laws, the commit gate, and the terminal segment stay as they are. So E2a's
closed-loop checks run on the existing worlds (the E1 gate and probes P1-P7,
which keep the [C3] forward mount). The `cone_up` mount gets fast tests only.
It flies closed loop in E2b, once a hold point below the target exists.

### E2a-1. Mount and lens from config (V-D1, E2-D2)
- `CameraModel` (`drone/camera.py`) already carries `r_body_cam`, `f_px`,
  `cx`, `cy`, and the companion core config already records it. Make the
  mount and lens a named, recorded config choice end to end: the companion
  config, the recording's `meta.config` [R2], and the harness `World`
  (which then owns its camera instead of the fixed [C3] default).
- Two named mounts. `c3_forward` is today's [C3] matrix. `cone_up` is
  E2-D2: optical axis along body -z (up at hover), image u along body x,
  image v along body -y:
  `R_body_cam = [[1,0,0],[0,-1,0],[0,0,-1]]`.
- The 100 deg lens: `f_px = 806` on 1920 px (`960 / tan(50 deg)` = 805.5),
  `cx`, `cy` unchanged. Half-angles 50.0 deg along u, 36.7 deg along v,
  54.5 deg at the corners. Uncalibrated (E1-F3).
- Edit [C3] to say the mount is configured, citing V-D1. Edit §9's camera
  row to list both mounts and lenses.
- Gate: the [S11] E1 fingerprint test still passes with `c3_forward` and
  `f = 1000`. Fast tests on `cone_up`: a target straight above a level
  vehicle lands at the image center; a target 40 deg above the horizon
  along body x lands at the u edge; along body y it is outside the frame;
  the config round-trips through the recording.

### E2a-2. Lens distortion (V-D11)
- Use the Brown-Conrady model (k1, k2, p1, p2, k3) on normalized image
  coordinates. That is the convention of `contracts/camera.py` (detection
  D0), so one ChArUco calibration serves both lines. The coefficients are
  config, default all zero.
- Undistort the box center and the box extents before [G1] and [G2] use
  them. `camera_sim` distorts what it projects with the same model.
- Gate: distort/undistort round trip on a grid inside the frame. With zero
  coefficients the E1 fingerprint is unchanged. With a placeholder
  (k1 = -0.3, labeled Provisional) one probe world still closes the loop and
  replays ([R3]).

### E2a-3. Tracker inside the companion (E2-D5)
- companiond runs the tracker in process. It binds the detection port
  (14601) and feeds each detection packet to the tracker, then the tracker's
  packets to the core, as the harness already does. It publishes track
  packets on 14602 for external listeners, the same way it publishes
  mission_state. The track packet stays the recorded track stream.
- Edit [C10] (two processes on the board: percepd and the companion),
  citing E2-D5.
- Settle and record in the PR: does [R3] replay re-run the tracker from the
  recorded detection packets and check the recorded track packets exactly?
  The tracker is plain float arithmetic and bit-identical run to run, so
  this should hold. If it does, replay covers the tracker.
- Gate: a companiond fast test sends detection packets on 14601 and sees
  track packets on 14602 and the expected mission transitions. Replay
  passes on a recorded probe run.

### E2a-4. World-angle tracking (V-D10)
- Undistort each box center (E2a-2) and de-rotate it with the attitude at
  its `t_cap` ([G1]) before the tracker filters it. The vehicle's own
  rotation then cancels out of association and prediction.
- Do not filter azimuth and elevation. With `cone_up` the view is centered
  on the zenith, where azimuth is singular. Filter in a tangent plane: each
  track keeps a reference direction (its first line of sight), maps each
  line of sight to gnomonic tangent-plane coordinates around it, and runs
  the [K1] per-axis filters there. Re-center the reference if the track
  drifts far from it. Box size stays a filtered quantity, in pixels or in
  angle; the PR chooses and records why.
- Association compares predicted and detected boxes in the tangent plane.
- Attitude-degraded frames: the PR chooses and records the behavior (for
  example, treat the frame as a miss for every track). Log it; do not build
  more than one path.
- Track packet, additive only (wire v1, [C7]): the line of sight in NED and
  its rate. The PR names the fields and adds golden fixtures. Old fixtures
  still decode. The PR also states what `u`, `v_px`, `du`, `dv` mean now (for
  example, the filtered line of sight re-projected with the attitude at
  `t_cap`). The UI and the [G4] centering test read them.
- [G1] then takes the tracker's line of sight instead of de-rotating `u`,
  `v_px` a second time, or proves the two agree. Record which.
- Edit [K1], [K2] and [G1], citing V-D10.
- E1-D2's trigger for Mahalanobis association ("recorded IoU failure in
  replay") has fired: V-S8 in PR #3 (P4 seed 4253150160: 39 plane
  detections made 39 one-hit tracks). World angles remove the self-rotation
  part. If P4 still fails on fast boxes after this work item, a
  center-distance or Mahalanobis gate for track birth is in scope, citing
  E1-D2. Measure first.
- Gate: a fast fixture with a static target while the vehicle yaws at
  `search_yaw_rate` (45 deg/s) and pitches +-10 deg keeps one track id for
  3 s. Re-run probes P1-P7 (3 probe seeds each) and report lock retention,
  one-hit track counts and results against the PR #3 baseline. This is a
  report, not a pass bar.

### E2a-5. Width rate and time to contact (V-D9)
- The [K1] width filter already estimates a rate. Publish it on the track
  packet (an additive field, px/s; the PR names it).
- Time to contact `tau = w / (dw/dt)` while the box grows and is not clipped
  at the frame edge; undefined otherwise. Compare it with [G2] size range
  when both exist, and log a mismatch (a counter or an event). A mismatch
  means the primed `W` is wrong (V-D9). E2a publishes and logs. E2b uses it.
- Gate: a synthetic approach at a known closing speed gives `tau` within a
  tolerance the PR declares before running the fixture.

### E2a hand-back
- The E1 gate, once, at the end: `run.sh gate <abs>/_local/sim-runs/<date>/gate
  --jobs 4 --speedup 2`. Green on both repeats.
- Fast tier green; whole-repo non-slow suite green except failures shown to
  predate the branch.
- The probe re-run table (E2a-4).
- Findings as proposed §11 rows.
- Proposed edits to section 5 so E2b's brief can be finalized.

## 5. Part E2b — the PN pass (outline, not sanctioned yet)
1. **Freeze the E2 gate first** (E2-D1). One PR, no guidance change.
   Scenes, pass numbers, and the seed rule are declared before the first
   gate run, and Samuel ratifies the numbers. Every scene flies the cone
   frame ([S12] `cone_x`), the `cone_up` mount, the 100 deg lens, touch
   trials, and targets 40-80 deg above the vehicle at search (E2-D6).
   Seeds: `sha256("E2-gate:<scene>:<i>")`, 20 for I1-I3, 3 for the others;
   `gate_repeats` 2. Draft scene list:

   | ID | Scene | Green when (numbers declared at freeze) |
   | --- | --- | --- |
   | I1 | Static balloon, calm air | commit, pass, `RETURN`, `LAND`; p95 commit-plane miss under the bar |
   | I2 | Tethered balloon swaying in steady wind plus turbulence (like P2) | as I1 |
   | I3 | Static balloon with bad frames: misses, drops, bursts, jitter (like P6) | as I1 |
   | I4 | Balloon plus clutter and a circling bird (like P5) | zero retargets; lock retention over the bar |
   | I5 | FC-link blackout at commit (like S4) | `RETURN`, RTL once the link returns, `LAND`; no floor breach |
   | I6 | Late abort during `TOUCH` (like S5) | `ABORT`, FC in `RTL`; abort latency under the bar |
   | I7 | Long dropout during the pass | track dies, brake, climb, `MISS`, `RETURN` (V-D12) |

   Add a per-run "crossed the commit plane" check to I1-I3 (V-S10). Do not
   check the `v_max` floor; report peak commanded and peak true speed
   (E2-D4).
2. **E2 probe set.** Cone-mount versions of the balloon probes in a new
   namespace (`E2-probe`) for development and tuning.
3. **Prove acceleration setpoints on SITL** before PN relies on them
   (E2-F1): a slow test sends each candidate mask and checks the response.
   Choose between 1031 (velocity plus acceleration, keeps ArduCopter's speed
   limit) and 1087 (acceleration only, no speed limit in the FC).
4. **fc_link [F6].** Add the chosen mask, with hard limits on what it
   sends (an acceleration limit as well as the velocity limits). [F1] is
   unchanged. FC parameters the operator would set (V-D7; for example
   `ANGLE_MAX`, `PSC_JERK_NE`) go into the SITL defaults file only.
5. **Hold point** (V-D3, V-D4): about 2 m from the target, below it, on the
   side the vehicle approaches from. The operator launches downwind of a
   tethered balloon so the string stays on the far side (V-D14). Proposal:
   the line from the hold point to the target about 70 deg above the
   horizon. Provisional. With `f = 806` and `W = 1 m` the box is 403 px at
   2 m (0.21 of the frame), so the [G4] fill clause needs `alpha` about
   0.18-0.2, or the commit gate changes (V-D4).
6. **PN pass** (V-D2) at 50 Hz (V-D6; `tick_hz` is 20 today, so the pass
   either ticks faster or steps on detection frames). Steer until the box
   touches the frame edge, the track is lost, or `tau` drops below about
   0.2 s, then hold the last command (V-D5). Brake, climb and `pass_done`
   stay. A dropout during the pass holds the last command up to `coast_cap`;
   a dead track ends the pass with brake, climb, `MISS` (V-D12).
7. **Done when:** the E2 gate is green on both repeats, the E1 gate is
   green, the fast tier is green, and every recording replays ([R3]).

## 6. Provisional defaults (new in E2)
- `cone_up` mount: `R_body_cam = [[1,0,0],[0,-1,0],[0,0,-1]]` (E2-D2).
- 100 deg lens: `f_px` 806, `cx` 959.5, `cy` 599.5; distortion zeros until
  calibrated (V-D11, E1-F3).
- Search view with `cone_up`, level: targets 40 deg or more above the
  horizon. With a height gap `dh` below the target, the vehicle sees it
  within about `1.19 * dh` horizontally (E2-D6).
- E2b, for reference: hold distance about 2 m (V-D4), hold elevation about
  70 deg, pass setpoints 50 Hz (V-D6), `tau` hand-off about 0.2 s (V-D5).

## 7. Fences
- The E1 brief §7 fences hold: fenced paths (v1/, golden/, proto/, the
  detection contracts), no gate or acceptance scenes as inputs, no in-flight
  frame recording, no real-FC sends, SITL only, no FC parameter writes from
  fc_link.
- Gate scenes S1-S6 and their seeds do not change. The E2 gate, once
  frozen, does not change without a recorded decision.
- Wire v1 stays: every packet change is additive.
- The parked detection line (D8, C-004) stays parked.

## 8. Open, not blocking E2a
- V-F5, rest: camera tilt and the yaw rotation between scan and pursuit;
  lens 100 deg vs 80-90 deg; the 3D sim design. (The wide image axis along
  body x, fore-aft, is confirmed: E2-D2.)
- E1-D3 (standoff geometry): parked with standoff trials (E2-D7). E1-F16
  (the hold under dropout) was closed on 2026-11-24 by E1-D14.
- E1-F15 (the `v_max` floor): closed on 2026-11-24 by E1-D13 for velocity
  commands. The E2 gate still does not check it (E2-D4).
- V-F4: no YOLO model and no percepd v0 source. The sim supplies boxes.
- V-S2: the cone frame's sim thrust-to-weight is 5, not the 8-10 V-F6
  estimates. The first real hover settles it.
- Lens calibration (ChArUco) and a measured mount (E1-F9).

## 9. Hand-back checklist
- One branch per work item or one for E2a; PRs opened as drafts; nothing
  pushed to main.
- Tests added for each work item, citing clauses and decision IDs.
- E1 gate result and the probe re-run table attached to the hand-back
  note.
- Findings listed as proposed §11 rows; an adversarial self-review done.
- Plain, short sentences in docs and PR text.
