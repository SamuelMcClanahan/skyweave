# SkyWeave drone stack contracts (E1 / D0)

**Revision:** 2026-10-08
**Status:** §2 (JSON wire v1) is FROZEN per `PHASE_E1_BRIEF.md` 3.7. §3-§8 are
behavior clauses written in phase E1, labeled clause by clause. Nothing in this
document is Measured. Numbers marked Provisional are prime-time defaults to be
tuned in the harness (brief §6), never on gate scenes.
**Scope:** Phase E1 work item 1. The single contract the drone stack's mission,
guidance, fc_link, tracker, harness, and ground UI code targets. Code never
targets another module's internals (brief §0).
**Change rule (fence):** this file is a fenced path, like
`DETECTION_CONTRACTS_D0.md`. After E1 merges, any clause here changes only with
a recorded decision appended to §11. Inside one wire version `v`, changes are
additive only; a breaking change bumps `v`.
**Test rule:** every drone test cites the clause ID it enforces (`[P3]`,
`[M7]`, ...), per `TESTING_DOCTRINE.md` rule 1. §10 maps clause series to tests.

Where the deployed percepd v0 / tracker v0 artifacts and this document
disagree, this document wins for v1 code. Their source is missing (brief §0),
so v0 behavior is not inherited, only the semantics this file restates.

---

## 1. Conventions (Chosen)

- **[C1] Clock.** Every time field in every packet and record is an integer
  count of milliseconds on the companion board's monotonic clock
  (`CLOCK_MONOTONIC` on the Cubie A7S; "board-monotonic ms"). percepd, the
  tracker, guidance, mission, fc_link, and the UI run on that one board, so
  every stream shares the domain. No packet carries wall-clock time. In SITL
  and replay the harness supplies the clock (simulation time); code reads time
  only through an injected clock, never directly.
- **[C2] Pixels.** Image coordinates are continuous pixels on the flight
  capture grid, 1920 x 1200 (brief §2). `(0.0, 0.0)` is the center of the
  top-left pixel, `u` right, `v` down (same convention as detection D0 §2). The
  640 x 360 sprint mode is reserved; it is not representable in v1.
- **[C3] Frames.** Vehicle body FRD (x forward, y right, z down). Local level
  frame NED, origin at the FC's EKF origin (home). Camera: OpenCV (+X right,
  +Y down, +Z along the optical axis). `R_A_B` maps vectors from frame B into
  frame A. Attitude angles are ArduPilot `ATTITUDE` roll/pitch/yaw, radians.
- **[C4] Units.** Meters, m/s, seconds, radians inside code. Packet fields carry
  the unit named in their table. `alpha` and `beta` are fractions of frame
  width (1920 px).
- **[C5] Encoding.** UTF-8 JSON, one packet per UDP datagram, one JSON object
  per packet. The canonical encoder writes ASCII only, sorted keys, separators
  `,` and `:`, no whitespace, finite numbers only (no NaN/Infinity). Senders
  that are not this repo's encoder may use any valid JSON layout; receivers
  must not depend on key order or whitespace.
- **[C6] Types.** An `int` field accepts only JSON integers (`1`, never `1.0`
  and never `true`). A `float` field accepts JSON integers or reals, never
  booleans. A `bool` field accepts only `true`/`false`. A `string` enum field
  accepts only the listed values.
- **[C7] Versioning.** Every packet carries integer field `v`; v1 is `1`.
  Receivers ignore unknown fields. A packet whose `v` is not `1` is rejected
  (counted and logged), never coerced. A missing required field, a wrong type,
  or an out-of-range value rejects the packet the same way.
- **[C8] Stream identity.** Packets carry no in-band type tag. Each packet
  kind travels on its own UDP port (defaults in §9, Provisional, config not
  contract). The recording (§3) wraps each packet with its stream name.
- **[C9] Size.** One datagram is at most 65507 bytes (the UDP payload limit).
  The canonical encoder refuses a packet that encodes larger. All v1 UDP
  traffic is board-local (loopback); the ground UI uses HTTP (§7).

## 2. JSON wire v1 (FROZEN)

Field names are exact. "Nullable" means the field is always present and may be
`null`. Ranges are enforced by the decoder per [C7].

### 2.1 detection packet (percepd -> tracker) [P1]

One packet per processed frame, including frames with no boxes.

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `t_cap` | int >= 0 | board ms | Capture time of the frame: the V4L2 buffer timestamp on `CLOCK_MONOTONIC`, as percepd v0 reports it. Its offset from mid-exposure is unmeasured (Provisional) |
| `frame_seq` | int >= 0 | - | Increases by at least 1 per frame within one percepd run. A gap is dropped frames. Consumers order by `t_cap`, not `frame_seq` |
| `boxes` | array of box | - | Zero or more boxes |

box:

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `x` | float | px | Left edge of the box on the flight grid [C2] |
| `y` | float | px | Top edge of the box |
| `w` | float > 0 | px | Box width |
| `h` | float > 0 | px | Box height |
| `conf` | float in [0, 1] | - | Detector confidence |

Box center is `(x + w/2, y + h/2)`. The top-left origin is Chosen in E1; the
v0 artifact's origin is unverified (finding E1-F1).

### 2.2 track packet (tracker -> guidance and mission) [P2]

One packet per live track per processed detection frame. All packets of one
frame share that frame's `t_cap`.

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `t_cap` | int >= 0 | board ms | `t_cap` of the detection frame this state belongs to |
| `track_id` | int >= 1 | - | Unique within one board boot; never reused, including across tracker restarts |
| `state` | string | - | `tentative`, `confirmed`, or `coasting` |
| `u` | float | px | Filtered box center, u |
| `v_px` | float | px | Filtered box center, v |
| `du` | float | px/s | Rate of `u` |
| `dv` | float | px/s | Rate of `v_px` |
| `w` | float > 0 | px | Filtered box width |
| `h` | float > 0 | px | Filtered box height |
| `hits` | int >= 0 | frames | Consecutive frames with an associated detection, ending at this frame (0 if this frame was a miss) |
| `misses` | int >= 0 | frames | Consecutive frames without an associated detection, ending at this frame (0 if this frame was a hit) |
| `age_frames` | int >= 1 | frames | Frames since birth, birth frame counted |

**[P2a] Track death.** A confirmed track coasts while `misses <= coast_cap`.
In the frame where `misses` would exceed `coast_cap`, the tracker emits one
final packet for it with `state = coasting` and `misses = coast_cap + 1`, then
deletes it; no later packet carries that id. A tentative track that fails
confirmation is deleted without a final packet. A consumer declares a track
dead on its final packet, or when no packet for that id arrives within
`track_timeout_ms` (§9; the tracker-crash backstop).

### 2.3 mission state packet (mission -> UI and recording) [P3]

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `t` | int >= 0 | board ms | Publish time |
| `mission_state` | string | - | One of `PRIMED`, `LAUNCH`, `SEARCH`, `ACQUIRING`, `ENGAGED`, `COASTING`, `TOUCH`, `COMPLETE`, `MISS`, `LOST`, `RETURN`, `LAND`, `ABORT` |
| `engaged_track_id` | int >= 1, nullable | - | The one engaged track (brief 3.2); `null` when none |
| `trial` | object | - | Trial parameter echo, below |
| `events` | array of event | - | Events since the previous mission state packet, in order. Each event appears in exactly one packet |

trial (echo of the accepted prime; all fields required):

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `trial_type` | string | - | `standoff` or `touch` |
| `v_max` | float > 0 | m/s | Closure / velocity cap |
| `alpha` | float in (0, 1] | - | Commit fill, `w / 1920` |
| `beta` | float in (0, 0.5] | - | Commit centering, offset / 1920 |
| `k` | int >= 1 | frames | Consecutive hits required to commit |
| `pass_budget` | int >= 1 | passes | Touch passes allowed |
| `search_alt` | float > 0 | m | Search altitude above home |
| `d_s` | float > 0 | m | Standoff distance |

event:

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `t` | int >= 0 | board ms | When the event happened |
| `name` | string, 1-128 chars | - | Event name, vocabulary in §4.6 |

**[P3a]** The mission publishes no mission state packet before its first
accepted prime: `PRIMED` is the first state that exists (§4.1).

### 2.4 fc_link health packet (fc_link -> mission, UI, recording) [P4]

Sent at about 1 Hz.

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `t` | int >= 0 | board ms | Publish time |
| `attitude_age_ms` | int >= 0, nullable | ms | `t` minus the receive time of the newest `ATTITUDE`; `null` if none was ever received |
| `fc_link_up` | bool | - | Any MAVLink message from the FC within `fc_link_bound_ms` (§9) |
| `rc_seen` | bool | - | `RC_CHANNELS` with `chancount > 0` within `fc_link_bound_ms` |
| `gate_state` | string | - | `locked` or `enabled` (the setpoint gate, [F5]) |
| `last_setpoint_t` | int >= 0, nullable | board ms | When the last velocity setpoint was written to the FC link; `null` if none |

### 2.5 command packet and ack (UI or radio -> mission) [P5]

command:

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `cmd_id` | string, 1-64 chars of `[A-Za-z0-9._:-]` | - | Sender-unique id |
| `token` | string, 1-256 chars | - | Shared secret |
| `command` | string | - | `prime`, `approve_engage`, `mark_complete`, or `abort` |
| `params` | object | - | Required for `prime` (prime params, below). Ignored for the other three |

prime params (validated by the mission; an invalid set is acked
`rejected_params`, never partly applied):

| Field | Type | Unit | Valid range |
| --- | --- | --- | --- |
| `trial_type` | string | - | `standoff` or `touch` |
| `v_max` | float | m/s | (0, `v_max_hard`] |
| `alpha` | float | - | (0, 1] |
| `beta` | float | - | (0, 0.5] |
| `k` | int | frames | [1, 1000] |
| `pass_budget` | int | passes | exactly 1 in v1 (brief 3.4: retry is v2, specced, not flown) |
| `search_alt` | float | m | (0, 120] |
| `d_s` | float | m | (0, 100] |
| `target_width_m` | float | m | (0, 10]; the known width `W` for size range (brief 3.5) |
| `engage_preauthorized` | bool | - | `true` gives engage authorization at prime time (brief §1) |
| `flight_time_cap_s` | float | s | (0, 1800] |
| `battery_floor_pct` | float | % | [0, 100] |
| `geofence_radius_m` | float | m | (0, 1000] |

ack (mission -> the command's sender):

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `cmd_id` | string | - | The command's `cmd_id` |
| `result` | string | - | `accepted`, `rejected_state`, `rejected_auth`, `rejected_params`, or `rejected_malformed` |

- **[P5a] At most once.** Each authenticated `cmd_id` executes at most once
  per mission process. A repeated authenticated `cmd_id` returns the stored
  ack unchanged and executes nothing, whatever its other fields say. A
  rejected command is stored the same way: a retry needs a new `cmd_id`.
  Commands that fail authentication are not stored.
- **[P5b] Never silent.** A command invalid in the current state is acked
  `rejected_state` and logged as an event (§4.6). A datagram that fails
  decoding is acked `rejected_malformed` when a valid `cmd_id` can be read
  from it, and is counted and logged in every case.
- **[P5c] Token.** The token is compared in constant time against the
  configured secret. It is never written to a recording or a log (§3).

## 3. Recording (Chosen, brief 3.8 / B5) [R1-R4]

- **[R1] Packets only, whole flight.** A recording is one JSONL file per
  mission process: one canonical JSON object per line [C5]. It carries
  packets, the full MAVLink log, and the mission loop's time inputs. It never
  carries image frames or pixels of any kind; no code path in the drone stack
  records frames in flight.
- **[R2] Records.** Every record has `t_rx` (board ms when the companion
  received or sent it) and `stream`:

  | `stream` | Other fields | Content |
  | --- | --- | --- |
  | `meta` | `format` = `skyweave-drone-rec`, `format_v` = 1, `config` (object) | First line. The configuration replay needs (mission and guidance constants, camera model) |
  | `detection`, `track`, `mission_state`, `fc_link_health`, `ack` | `pkt` (the packet object) | Packets as on the wire |
  | `command` | `pkt` (the command object without `token`), `auth_ok` (bool) | Commands as received; the token is removed and replaced by the authentication result |
  | `mavlink` | `dir` (`rx` or `tx`), `raw` (base64 of the MAVLink2 frame bytes) | Every MAVLink frame the companion receives or sends |
  | `tick` | - | One mission-loop tick (drives time-based transitions) |
  | `ground_hb` | - | One ground-link heartbeat (a UI poll or an authenticated command) |

- **[R3] Replay determinism.** Feeding a recording's input records
  (`track`, `command`, `mavlink` rx, `tick`, `ground_hb`) in file order into a
  fresh companion core built from its `meta.config` reproduces its
  `mission_state` and `ack` records exactly, and the velocity setpoints in its
  `mavlink` tx records exactly.
- **[R4] Miss vector offline.** The commit miss vector [G6] is computable from
  a recording alone: the engaged track packet that triggered the `commit`
  event plus the `meta.config` camera model and the primed `target_width_m`.

## 4. Mission state machine (Chosen, brief 3.2-3.4)

### 4.1 States and inputs

- **[M1] States** are the 13 values of `mission_state` [P3]. Before the first
  accepted prime the mission is unprimed: it has no state and publishes no
  state packet. Airborne set `A` = {`LAUNCH`, `SEARCH`, `ACQUIRING`,
  `ENGAGED`, `COASTING`, `TOUCH`, `LOST`}.
- **[M2] Inputs**, each stamped with board ms: commands with their
  authentication result; track packets; vehicle snapshots derived from the FC's
  MAVLink stream (mode, armed, landed state, altitude above home, horizontal
  distance from home, battery percent, link and attitude health); guidance
  events (`commit`, `pass_done`, `hold_complete`); ground heartbeats; ticks.
  The mission core is pure logic: the same inputs in the same order give the
  same outputs [R3]. It never reads a clock.
- **[M3] Sources** (who may trigger): `HUMAN` (UI command, radio approve
  switch, radio mode switch), `TRACKER`, `GUIDANCE`, `VEHICLE` (FC telemetry
  progress), `BUDGET`, `FAILSAFE`, `AUTO` (a follow-on transition on the next
  input, with no new trigger).
- **[M4] Precedence** when one input triggers more than one transition:
  ABORT > FAILSAFE > BUDGET > HUMAN `mark_complete` > GUIDANCE > TRACKER >
  VEHICLE > AUTO. ABORT is reachable from every state and always wins.

### 4.2 Transition table (who may trigger)

Every transition the code makes is a row here; any other change is a defect.

| ID | From | To | Trigger | Source |
| --- | --- | --- | --- | --- |
| T01 | unprimed | `PRIMED` | `prime` accepted | HUMAN |
| T02 | `PRIMED` | `PRIMED` | `prime` accepted while disarmed (replaces the trial) | HUMAN |
| T03 | `PRIMED` | `LAUNCH` | Vehicle snapshot: mode `GUIDED` (pilot set the engage gate), FC link up, attitude fresh, on the ground | HUMAN (radio mode switch, seen through VEHICLE) |
| T04 | `LAUNCH` | `SEARCH` | Vehicle in air and altitude >= `takeoff_done_frac` x `search_alt` | VEHICLE |
| T05 | `SEARCH` | `ACQUIRING` | A track packet with `state = confirmed`; that `track_id` becomes the candidate | TRACKER |
| T06 | `ACQUIRING` | `SEARCH` | Candidate death [P2a] | TRACKER |
| T07 | `ACQUIRING` | `ENGAGED` | `approve_engage` accepted, or `engage_preauthorized` was primed (then T07 follows T05 on the same input); `engaged_track_id` := candidate | HUMAN |
| T08 | `ENGAGED` | `COASTING` | Engaged track packet with `state = coasting` | TRACKER |
| T09 | `COASTING` | `ENGAGED` | Engaged track packet with `state = confirmed` | TRACKER |
| T10 | `ENGAGED`, `COASTING` | `LOST` | Engaged track death [P2a]; `engaged_track_id` := null | TRACKER |
| T11 | `ENGAGED` | `TOUCH` | `commit` (touch trials only) | GUIDANCE |
| T12 | `ENGAGED` | `COMPLETE` | `hold_complete` (standoff trials only) | GUIDANCE |
| T13 | `SEARCH`, `ACQUIRING`, `ENGAGED`, `COASTING`, `TOUCH`, `LOST` | `COMPLETE` | `mark_complete` accepted | HUMAN |
| T14 | `TOUCH` | `MISS` | `pass_done` without a completion mark | GUIDANCE |
| T15 | `LOST` | `SEARCH` | Next input; opens the reacquire window (`reacquire_window_ms`) | AUTO |
| T16 | `COMPLETE`, `MISS` | `RETURN` | Next input (v1: one pass, unconditional RTL) | AUTO |
| T17 | `A` | `RETURN` | Budget: flight time since LAUNCH > `flight_time_cap_s`; battery < `battery_floor_pct`; distance from home > `geofence_radius_m`; or the reacquire window expired before `ENGAGED` | BUDGET |
| T18 | `A` | `RETURN` | Link loss: FC link down (`fc_link_up` false), or no ground heartbeat for `ground_link_timeout_ms` | FAILSAFE |
| T19 | every state, and unprimed | `ABORT` | `abort` accepted | HUMAN |
| T20 | `A` | `ABORT` | FC mode leaves `GUIDED` while the FC link is up (radio hard abort, or an FC failsafe) | FAILSAFE |
| T21 | `ABORT` | `RETURN` | Next input, vehicle airborne | AUTO |
| T22 | `ABORT` | `LAND` | Next input, vehicle on the ground | AUTO |
| T23 | `RETURN` | `LAND` | FC reports landing or on ground, or mode `LAND` | VEHICLE |
| T24 | `LAND` | `PRIMED` | `prime` accepted while disarmed (next trial) | HUMAN |

- **[M5] Command validity.** `prime`: unprimed, or `PRIMED`/`LAND` while
  disarmed. `approve_engage`: `ACQUIRING` only. `mark_complete`: the T13 "from"
  states. `abort`: always (unprimed, an accepted abort is logged and changes
  nothing because no state exists). Anything else is `rejected_state` [P5b].
- **[M6] v1 MISS.** In v1, `MISS` means "the pass finished without an onboard
  completion mark". It is not a hit/miss verdict: that verdict is decided on
  the ground by the human plus replay (brief 3.4). Autonomous MISS handling
  and retry are v2 and are rejected at prime (`pass_budget` must be 1).

### 4.3 Lock stickiness (brief 3.2)

- **[M7]** `engaged_track_id` is set only by T07 (human approval) and cleared
  only by engaged-track death, or replaced only by an accepted re-prime
  (T02/T24, a human command). No transition, score, or new track changes it.
  A higher-scoring, nearer, or newer track never retargets guidance.
- **[M8]** The `ACQUIRING` candidate is sticky the same way: chosen once by
  T05, it changes only by its death (T06). `approve_engage` engages the
  candidate the UI was showing, never a newer track.
- **[M9]** Engaged-track death in `TOUCH`, `COMPLETE`, `MISS`, `RETURN`,
  `LAND`, or `ABORT` clears `engaged_track_id` and causes no transition.

### 4.4 Exits (brief 3.3)

- **[M10]** Every exit is an FC-owned mode change. Entering `RETURN` requests
  `RTL`; entering `LAND` from `ABORT` on the ground requests `LAND` if armed.
  The companion issues these requests only while the FC is in `GUIDED`
  ([F9]); it never overrides a mode the pilot or an FC failsafe selected.
- **[M11]** The Wi-Fi `abort` is a soft abort: a request. The radio mode
  switch is the hard abort and works with the companion dead (FC-owned, not
  companion code).

### 4.5 Effects

- **[M12]** On entering `LAUNCH` the mission requests arm and takeoff to
  `search_alt`. On entering `RETURN` it requests `RTL` [M10]. On entering a
  state listed in the tone table (§9) it requests that tone. The mission
  requests nothing else from the FC; velocity setpoints come from guidance.

### 4.6 Event vocabulary (Chosen; receivers ignore unknown names)

| Name | When |
| --- | --- |
| `transition:<FROM>-><TO>` | Every state change; `<FROM>` is `UNPRIMED` for T01 |
| `candidate:<id>` | T05 sets the candidate |
| `engaged:<id>` | T07 sets `engaged_track_id` |
| `track_dead:<id>` | Candidate or engaged-track death |
| `commit`, `pass_done`, `hold_complete` | Guidance events, when the mission accepts them |
| `cmd:<command>:<result>` | Every command, with its ack result |
| `budget:<flight_time|battery|geofence|reacquire>` | T17 |
| `failsafe:<fc_link|ground_link|mode>` | T18, T20 |
| `tone:<name>` | A tone request |

## 5. Guidance v1 (Chosen; numbers Provisional, brief 3.5-3.6)

- **[G1] De-rotation.** A track's line of sight in NED is
  `los = R_ned_body(att(t_cap)) . R_body_cam . normalize(ray)`, with
  `ray = ((u - cx)/f, (v_px - cy)/f, 1)` and `att(t_cap)` the FC attitude
  interpolated at the track's `t_cap` from timestamped `ATTITUDE` samples
  ([F2]; yaw unwrapped). If no sample lies within `attitude_bound_ms` of
  `t_cap`, the attitude is degraded for that packet [G8].
- **[G2] Size range** (brief 3.5). `Z = f * W / w` with `W` the primed
  `target_width_m`; range along the line of sight `r = Z * |ray|`. Valid when
  `w >= w_min_px`. Otherwise the assume-and-bound fallback applies:
  `r = clamp(r_assume, r_min, r_max)`, and the range source is "assumed".
  Onboard scope is bearing plus size range only; no 3D target estimation.
- **[G3] Pure pursuit** (pluggable law; v1 law). Standoff
  `s = d_s` in standoff trials and `s = 0` in touch trials (the commit gate
  [G4] ends the approach). Error `e = r - s`. Along-LOS speed
  `v_a = Kp * e`, zero inside `|e| <= deadband_m`. Command
  `v = v_a * los`, then: clamp `|v| <= v_max` (trial), then rate-limit
  `|v_k - v_(k-1)| <= a_max * dt`. Yaw rate is `K_yaw` times the body-frame
  azimuth of `los`, clamped to `yaw_rate_max`, so the body-fixed camera keeps
  the target in view. The law sits behind an interface that takes the
  de-rotated observation and returns a velocity command; the harness ranks
  laws by scorecard.
- **[G4] Commit gate** (brief 3.4). In `ENGAGED`, touch trials only, on an
  engaged track packet: commit when `w / 1920 >= alpha` AND
  `hypot(u - cx, v_px - cy) <= beta * 1920` AND `state = confirmed` AND
  `hits >= k` AND attitude not degraded. At most one commit per pass.
- **[G5] Terminal segment.** At commit: contact point `c = r * los` (NED,
  relative to the vehicle), closure speed `v_c = v_max` (trial cap). Fly
  `v_c * c / |c|` open loop (no tracker input) for `|c| / v_c + t_overrun_s`,
  then brake (zero velocity) for `t_brake_s`, then climb at `v_climb` for
  `t_climb_s`, then emit `pass_done`. The pass ignores track packets.
- **[G6] Miss vector at commit.** `eps = (u - cx, v_px - cy)` px of the
  committing packet; `miss = Z * eps / f` m (camera right, down), with `Z` from
  [G2]. Computed and logged at commit, and recomputable from the recording
  [R4]. It is the predicted miss of a pass flown along the boresight.
- **[G7] Standoff hold.** In `ENGAGED`, standoff trials: `hold_complete` when
  `|r - d_s| <= hold_tol_m` holds without a break for `hold_time_s`. A
  coasting packet or degraded attitude breaks the hold.
- **[G8] Degraded attitude.** While attitude is degraded, guidance commands
  zero velocity and zero yaw rate, and the commit gate stays shut.
- **[G9] Lock.** Guidance consumes only packets whose `track_id` equals
  `engaged_track_id` (`ENGAGED`, `COASTING`) or the candidate (`ACQUIRING`,
  yaw only). Every other packet is ignored [M7].
- **[G10] Per-state output.** `SEARCH`: zero velocity, yaw rate
  `search_yaw_rate`. `ACQUIRING`: zero velocity, yaw toward the candidate.
  `ENGAGED`: [G3]. `COASTING`: zero velocity, yaw toward the coasting
  prediction. `LOST`: zero velocity, zero yaw rate. `TOUCH`: [G5]. All other
  states and unprimed: no setpoints at all.

## 6. fc_link v1 (Chosen; numbers Provisional)

- **[F1] SITL-only interlock (phase E1).** fc_link accepts only loopback
  MAVLink endpoints (`tcp:127.0.0.1:<port>`, `udp...:127.0.0.1:<port>`). A
  serial device or a non-loopback host is refused at construction. No code
  path in E1 can arm, command, or change a parameter on a real FC. fc_link has
  no parameter-write code path at all; SITL is configured through its defaults
  file.
- **[F2] Attitude subscription.** On connect, fc_link requests `ATTITUDE` at
  50 Hz with `MAV_CMD_SET_MESSAGE_INTERVAL`, plus the telemetry the vehicle
  snapshot needs. Each `ATTITUDE` is stored with its board-ms receive time and
  its FC `time_boot_ms`. At least 1 s of samples is kept for [G1].
- **[F3] Staleness.** `attitude_age_ms` = now minus the newest `ATTITUDE`
  receive time. Attitude is degraded when the age exceeds `attitude_bound_ms`
  or no sample exists. `fc_link_up` per [P4].
- **[F4] Health packet** [P4] at about 1 Hz.
- **[F5] Setpoint gate.** The gate is `locked` by default. Only an explicit
  configuration value enables it. While locked, a velocity send, an arm, or a
  takeoff writes zero bytes to the link and is counted as blocked.
- **[F6] Setpoint clamps.** Independent of guidance, every sent setpoint is
  clamped to `v_xy_hard`, `v_z_hard`, and `yaw_rate_hard`. Setpoints use
  `SET_POSITION_TARGET_LOCAL_NED`, frame `LOCAL_NED`, velocity-only type mask
  (plus yaw rate when given), and are re-sent at `setpoint_hz` (>= 2 Hz).
- **[F7] RC passthrough.** `RC_CHANNELS` is read; a rising edge of the
  approve channel through `approve_pwm_high` is an approve input (a HUMAN
  `approve_engage`). fc_link never sends RC overrides.
- **[F8] Tones.** `PLAY_TUNE` with the tune for a tone name from the §9
  table; an unknown name sends nothing.
- **[F9] Exit requests.** `RTL` and `LAND` mode requests are sent only while
  the FC reports `GUIDED`.
- **[F10] MAVLink log.** Every MAVLink frame received or sent is handed to the
  recorder as raw bytes [R2].
- **[F11] Clock injection.** fc_link reads time only through its injected
  clock [C1].

## 7. Ground UI (Chosen)

- **[U1]** Served over HTTP by the companion process; a laptop browser is the
  client. Endpoints: the page, a JSON state view, and a command endpoint that
  takes a command packet [P5] and returns its ack.
- **[U2]** Exactly four commands: `prime` (with the parameter form),
  `approve_engage`, `mark_complete`, `abort`. No joystick, no manual velocity,
  no other command, ever. Any other command name is refused.
- **[U3]** Ack by id [P5a] and a shared-secret token [P5c]. The page never
  stores the token beyond its own memory.
- **[U4]** The page shows the state banner, engaged track (and the candidate
  awaiting approval), battery, and link health (FC link, attitude age, RC,
  gate state, ground link), plus the trial echo and recent events.
- **[U5]** The page renders from a recorded packet stream: the same render
  function serves live state and a replayed recording.
- **[U6]** Each page state poll is a ground heartbeat [R2].

## 8. Closed-loop harness and scorecard (Chosen)

- **[S0] Loop.** Synthetic target trajectory -> camera projection (noise,
  dropout, injected boresight error) -> detection packets -> tracker ->
  guidance and mission -> fc_link -> ArduCopter 4.7.0 SITL, closed through the
  SITL vehicle's pose. Time is SITL time [C1]. Every run takes an explicit
  seed; scored outputs carry no wall-clock values.
- **Tracker.** If tracker v0 source is not recovered, the harness uses a
  reimplementation of the brief §2 spec, labeled Provisional; its v0 numbers
  (1.4 px RMS, zero ghosts) are not inherited and must be re-earned [K1-K4].
- **Scenarios** (each green under its fixed seeds):

  | ID | Scenario | Green when |
  | --- | --- | --- |
  | S1 | Nominal standoff hold, static balloon | `COMPLETE` by [G7]; true standoff error p95 <= `hold_tol_m` over the hold; `RETURN` then `LAND` |
  | S2 | Touch pass, static balloon | Commit, pass, `MISS`/`COMPLETE`, `RETURN`, `LAND`; p95 miss at the commit plane <= `miss_p95_max_m` over the seed set |
  | S3 | Detection dropout | Short dropout (< coast cap): lock retained, same `engaged_track_id`. Long dropout (> coast cap): `LOST`, `SEARCH`, reacquire within the window |
  | S4 | Link loss at commit | FC-link blackout at commit: `RETURN`, RTL once the link returns, `LAND`; no safety-floor breach |
  | S5 | Late abort | `abort` during `TOUCH`: `ABORT`, `RETURN`, FC in `RTL`; abort latency <= `abort_latency_max_ms` |
  | S6 | Target maneuver | Kite that reverses direction, plus a crossing distractor: lock retention >= `lock_retention_min`; zero retargets |

- **Scorecard JSON per run:** scenario, seed, backend and versions, law, p95
  miss at the commit plane, standoff hold error, lock retention, abort latency,
  safety floors (geofence breach, minimum altitude), final state, transition
  list, pass/fail per check. Commit plane: the plane through the target at
  closest approach, normal to the terminal velocity; miss is the distance from
  the target center to where the vehicle crosses it.
- **Safety floors:** no geofence breach (true horizontal distance from home
  <= `geofence_radius_m`); no altitude below `alt_floor_m` outside takeoff and
  landing; commanded speed never above `v_max`.

## 9. Provisional defaults

From brief §6 unless marked "E1". All Provisional.

| Name | Value | Where |
| --- | --- | --- |
| `v_max` | 2.5 m/s (touch trials) | prime |
| `d_s` | 5 m | prime |
| `hold_tol_m`, `hold_time_s` | 1.0 m, 10 s | [G7] |
| `alpha`, `beta`, `k` | 0.4, 0.1, 5 | prime |
| tracker M of N, `coast_cap` | 3 of 5, 20 frames | [K2], [P2a] |
| `pass_budget` | 1 | prime |
| `search_yaw_rate` | 45 deg/s | [G10] |
| `reacquire_window_ms` | 10000 | T15, T17 |
| attitude rate, `attitude_bound_ms`, `setpoint_hz` | 50 Hz, 100 ms, 10 Hz | [F2], [F3], [F6] |
| `search_alt` | 10 m (E1) | prime |
| `target_width_m` | 1.0 m (E1) | prime |
| `engage_preauthorized` | false (E1) | prime |
| `flight_time_cap_s`, `battery_floor_pct`, `geofence_radius_m` | 180 s, 30 %, 60 m (E1) | prime |
| `v_max_hard` | 5.0 m/s (E1) | prime validation |
| `track_timeout_ms` | 500 (E1) | [P2a] |
| `fc_link_bound_ms` | 1000 (E1) | [P4] |
| `ground_link_timeout_ms` | 5000 (E1) | T18 |
| `takeoff_done_frac` | 0.9 (E1) | T04 |
| camera `f`, `cx`, `cy`, `R_body_cam` | 1000 px, 959.5, 599.5, camera +Z = body +x (E1; AR0234 lens uncalibrated) | [G1] |
| `w_min_px`, `r_assume`, `r_min`, `r_max` | 4 px, 15 m, 1 m, 60 m (E1) | [G2] |
| `Kp`, `deadband_m`, `a_max` | 0.8 1/s, 0.25 m, 2.0 m/s^2 (E1) | [G3] |
| `K_yaw`, `yaw_rate_max` | 1.5 1/s, 90 deg/s (E1) | [G3] |
| `t_overrun_s`, `t_brake_s`, `v_climb`, `t_climb_s` | 0.5 s, 1.0 s, 1.5 m/s, 2.0 s (E1) | [G5] |
| `v_xy_hard`, `v_z_hard`, `yaw_rate_hard` | 5.0 m/s, 2.0 m/s, 90 deg/s (E1) | [F6] |
| `approve_channel`, `approve_pwm_high` | RC 8, 1700 us (E1) | [F7] |
| `mission_publish_hz` | 5 Hz plus every state change (E1) | [P3] |
| UDP ports | detection 14601, track 14602, mission_state 14603, fc_link_health 14604, command 14605 (E1) | [C8] |
| `miss_p95_max_m` | 0.5 m (E1, declared before the first run) | S2 |
| `abort_latency_max_ms` | 500 (E1, declared before the first run) | S5 |
| `lock_retention_min` | 0.95 (E1, declared before the first run) | S6 |
| `alt_floor_m` | 2.0 m (E1) | safety floor |

Tone table (E1, [F8], [M12]): `ACQUIRING`, `ENGAGED`, `TOUCH`, `LOST`,
`RETURN`, `ABORT` each have a distinct short tune; other states have none.

## 10. Done when (tests)

| Series | Clauses | Content | Tier |
| --- | --- | --- | --- |
| P | [C5]-[C9], [P1]-[P5] | Golden packet fixtures beside the tests; byte-exact canonical encoding; decode/encode round-trip; unknown-field tolerance; version, type, and range rejection | fast |
| R | [R1]-[R4] | Recording round-trip; token never recorded; replay of a recorded stream reproduces mission states, acks, and setpoints | fast |
| M | [M1]-[M12] | Every transition T01-T24 and every command rejection; precedence; lock stickiness against a higher-scoring track | fast |
| K | [K1]-[K4], [P2a] | Provisional tracker: M-of-N confirm, coast cap and final packet, id uniqueness across restarts | fast |
| G | [G1]-[G10] | Synthetic geometry: known offsets give known commands, commit decisions, and miss vectors | fast |
| F | [F1]-[F11] | Interlock refusal, gate provably blocks sends, clamps, staleness via injected clock; SITL scripted run (arm, takeoff, velocity, land) and injected attitude staleness | fast + slow (SITL) |
| U | [U1]-[U6] | Headless: command idempotency, invalid-state rejection, four commands only, render from a recorded stream | fast |
| S | [S0]-[S6] | Closed-loop SITL scenarios under fixed seeds, scorecard emitted | slow (SITL) |

Tracker clauses (Provisional reimplementation, brief §2 spec):

- **[K1]** Per-axis 2-state Kalman filter `[pos, vel]`, constant velocity, on
  box center u, v and size w, h.
- **[K2]** IoU association; a track confirms after M hits in its first N
  frames.
- **[K3]** A confirmed track coasts on a miss and dies per [P2a].
- **[K4]** `track_id` uniqueness per [P2]: ids start from a base derived from
  the tracker's start time on the board clock.

## 11. Findings and decisions log

| ID | Item | Label |
| --- | --- | --- |
| E1-D1 | JSON wire v1 frozen (§2), per brief 3.7, 2026-10-08 | Chosen |
| E1-D2 | Deferred with triggers: binary packets (measured serialization cost), Mahalanobis association (recorded IoU failure in replay), IMM (recorded CV-model failure on a real maneuver) | Deferred |
| E1-D3 | Touch-trial pursuit standoff is 0 ([G3]): with `alpha` = 0.4 and `f` = 1000 px, a 1 m target fills 40 % of the width at 1.3 m, so a 5 m standoff could never satisfy [G4] | Chosen |
| E1-F1 | Box origin (top-left vs center) of the deployed percepd v0 is unverified; v1 is top-left [P1]. Check against a v0 capture on the Cubie before percepd v1 | Finding |
| E1-F2 | percepd v0 and tracker v0 emit no `v` field; their packets are not v1 packets. percepd v1 adds it (brief work item 7) | Finding |
| E1-F3 | AR0234 lens intrinsics are uncalibrated; `f`, `cx`, `cy` are placeholders. Boresight calibration from logged miss vectors is a new open question (working doc §15) | Finding |
