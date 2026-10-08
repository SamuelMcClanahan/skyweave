# SkyWeave drone stack contracts (E1 / D0)

**Revision:** 2026-10-08
**Status:** §2 (JSON wire v1) is FROZEN per `PHASE_E1_BRIEF.md` 3.7. §1 and
§3-§8 are behavior clauses written in phase E1, labeled clause by clause.
Nothing in this document is Measured. Numbers marked Provisional are
prime-time defaults to be tuned in the harness on probe seeds only ([S7]),
inside a campaign file, never on gate seeds.
**Scope:** Phase E1 work item 1. The single contract the drone stack's mission,
guidance, fc_link, tracker, harness, and ground UI code targets. Code never
targets another module's internals (brief §0).
**Change rule (fence):** this file is a fenced path, like
`DETECTION_CONTRACTS_D0.md`. After E1 merges, any clause here changes only with
a recorded decision appended to §11. Inside one wire version `v`, changes are
additive only; a breaking change bumps `v`.
**Test rule:** every drone test cites the clause ID it enforces (`[P3]`,
`[M7]`, `T07`, ...), per `TESTING_DOCTRINE.md` rule 1. §10 maps clause series
to tests.

The source of percepd v0 and tracker v0 is missing (brief §0). v1 code
targets this document. The detection packet keeps the v0 field set and
meanings named in brief §4; where v0 behavior is unverified, §11 records it
as a finding.

---

## 1. Conventions (Chosen)

- **[C1] Clock.** Every time field in every packet and record is an integer
  count of milliseconds on the companion board's monotonic clock
  (`CLOCK_MONOTONIC` on the Cubie A7S; "board ms"). percepd, the tracker, and
  the companion process run on that one board, so every stream shares the
  domain. No packet carries wall-clock time. In SITL and replay the harness
  supplies the clock (SITL time). Code reads time only through an injected
  clock, never directly.
- **[C2] Pixels.** Image coordinates are continuous pixels on the flight
  capture grid, 1920 x 1200 (brief §2). `(0.0, 0.0)` is the center of the
  top-left pixel, `u` right, `v` down (same convention as detection D0 §2). The
  640 x 360 sprint mode is reserved; it is not representable in v1.
- **[C3] Frames.** Vehicle body FRD (x forward, y right, z down). Local level
  frame NED, origin at the FC's EKF origin (home). Camera: OpenCV (+X right,
  +Y down, +Z along the optical axis). `R_A_B` maps vectors from frame B into
  frame A. Attitude is ArduPilot `ATTITUDE` roll, pitch, yaw (radians), with
  `R_ned_body = Rz(yaw) Ry(pitch) Rx(roll)`. The camera mount is upright with
  no uptilt: `R_body_cam = [[0,0,1],[1,0,0],[0,1,0]]` (body x = camera Z,
  body y = camera X, body z = camera Y).
- **[C4] Units.** Meters, m/s, seconds, radians inside code. Packet fields carry
  the unit named in their table. `alpha` and `beta` are fractions of frame
  width (1920 px). Positive yaw rate turns the nose right (clockwise seen
  from above), as in MAVLink `LOCAL_NED`.
- **[C5] Encoding.** UTF-8 JSON, one packet per UDP datagram, one JSON object
  per packet. The canonical encoder writes ASCII only (non-ASCII escaped),
  sorted keys, separators `,` and `:`, no whitespace, finite numbers only (no
  NaN, Infinity, or a literal that overflows to infinity). Receivers must not
  depend on key order or whitespace.
- **[C6] Types.** An `int` field accepts only JSON integers (`1`, never `1.0`
  and never `true`). A `float` field accepts JSON integers or reals, never
  booleans. A `bool` field accepts only `true`/`false`. A `string` enum field
  accepts only the listed values.
- **[C7] Versioning.** Every packet carries integer field `v`; v1 is `1`.
  Receivers ignore unknown fields. A packet whose `v` is not `1` is rejected
  (counted and logged), never coerced. A missing required field, a wrong type,
  an out-of-range value, or input that is not decodable JSON rejects the
  packet the same way: the decoder raises its one rejection error and nothing
  else.
- **[C8] Stream identity.** Packets carry no in-band type tag. Each packet
  kind travels on its own UDP port (defaults in §9, Provisional, config not
  contract). The recording (§3) wraps each packet with its stream name.
- **[C9] Size.** One datagram is at most 65507 bytes (the UDP payload limit).
  The canonical encoder refuses a packet that encodes larger.
- **[C10] Process topology.** The board runs three processes: percepd, the
  tracker, and the companion. The companion process holds mission, guidance,
  fc_link, the UI server, and the recorder; hand-offs inside it are
  in-process calls, not datagrams. "Mission process" in this document means
  the companion process. The pure-logic part of it (mission plus guidance plus
  the vehicle-state parser) is the companion core of [R3]. Each UDP port has
  exactly one bound receiver; `SO_REUSEPORT` fan-out is not used. detection
  goes to the tracker; track and command go to the companion; the companion
  publishes mission_state and fc_link_health on their ports for external
  listeners.

## 2. JSON wire v1 (FROZEN)

Field names are exact. "Nullable" means the field is always present and may be
`null`. Ranges are enforced by the decoder per [C7].

### 2.1 detection packet (percepd -> tracker) [P1]

One packet per processed frame, including frames with no boxes.

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `t_cap` | int >= 0 | board ms | Capture time of the frame on the board clock. Its source in percepd v0 and its offset from mid-exposure are unverified (Provisional, E1-F1) |
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

Box center is `(x + w/2, y + h/2)`. The top-left origin is the v1 definition.
Whether percepd v0 emits top-left or center is unverified (E1-F1); percepd v1
converts at its encoder if needed, and [P1] does not change.

### 2.2 track packet (tracker -> companion) [P2]

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

**[P2a] Track death.** `coast_cap` is one configuration value shared by the
tracker and the companion (it is in the recording's `meta.config` [R2]). A
confirmed track coasts while `misses <= coast_cap`. In the frame where
`misses` would exceed `coast_cap`, the tracker emits one final packet with
`state = coasting` and `misses = coast_cap + 1`, then deletes the track; no
later packet carries that id. A tentative track that fails confirmation is
deleted without a final packet. A consumer declares a track dead on a packet
with `state = coasting` and `misses > coast_cap`, or when no packet for that id
arrives within `track_timeout_ms` (§9; the tracker-crash backstop). A death by
timeout ends that id's candidate or engaged role; if packets for the id
resume, they are ordinary track packets (a later T05 may pick it again).

### 2.3 mission state packet (companion -> UI, recording, listeners) [P3]

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `t` | int >= 0 | board ms | The stamp of the input that caused this publish [M2] |
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
| `t` | int >= 0 | board ms | The stamp of the input that caused the event [M2] |
| `name` | string, 1-128 chars | - | Event name, vocabulary in §4.6 |

**[P3a]** The mission publishes no mission state packet and records no event
before its first accepted prime: `PRIMED` is the first state that exists
(§4.1). While unprimed, the `command` and `ack` records [R2] are the only log.
The first packet's events start with `cmd:prime:accepted` and
`transition:UNPRIMED->PRIMED`.

### 2.4 fc_link health packet (companion -> UI, recording, listeners) [P4]

Sent at about 1 Hz.

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `t` | int >= 0 | board ms | Publish time (fc_link's injected clock) |
| `attitude_age_ms` | int >= 0, nullable | ms | `t` minus the receive time of the newest `ATTITUDE`; `null` if none was ever received |
| `fc_link_up` | bool | - | Any MAVLink message from the FC within `fc_link_bound_ms` (§9) |
| `rc_seen` | bool | - | `RC_CHANNELS` with `chancount > 0` within `fc_link_bound_ms` |
| `gate_state` | string | - | `locked` or `enabled` (the setpoint gate, [F5]) |
| `last_setpoint_t` | int >= 0, nullable | board ms | When the last velocity setpoint was written to the FC link; `null` if none |

### 2.5 command packet and ack (UI or radio -> companion) [P5]

command:

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `cmd_id` | string, 1-64 chars of `[A-Za-z0-9._:-]` | - | Unique across all senders for the life of the mission process, including sender restarts and page reloads. The UI draws a fresh random id per command (for example `crypto.randomUUID()`); a counter is not allowed. The prefix `rc:` is reserved for radio approvals [F7] |
| `token` | string, 1-256 chars of printable ASCII `[!-~]` | - | Shared token |
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

ack (companion -> the command's sender):

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `v` | int | - | `1` |
| `cmd_id` | string | - | The command's `cmd_id` |
| `result` | string | - | `accepted`, `rejected_state`, `rejected_auth`, `rejected_params`, `rejected_malformed`, or `rejected_duplicate_id` |

- **[P5a] At most once.** Each authenticated `cmd_id` executes at most once
  per mission process. The mission stores each ack with a fingerprint of the
  command's `command` and canonical `params` (never the token). A repeated
  authenticated `cmd_id` with the same fingerprint returns the stored ack and
  executes nothing (a true retry). A repeated `cmd_id` with a different
  fingerprint executes nothing, leaves the stored entry unchanged, and is acked
  `rejected_duplicate_id`; the sender re-sends under a fresh id. A rejected
  command is stored like any other. Commands that fail authentication are not
  stored. `abort` is not exempt.
- **[P5b] Never silent.** A command invalid in the current state is acked
  `rejected_state` and logged as a `cmd:` event (§4.6) when a state exists
  ([P3a] otherwise). A datagram that fails decoding never reaches the mission
  core: the command receiver acks it `rejected_malformed` when a `cmd_id` can
  be salvaged, writes that ack to the recording, and counts and logs it in the
  process log in every case. It is never stored in the [P5a] table and is not
  a ground heartbeat.
- **[P5c] Token.** The configured token satisfies the same pattern as the
  field. The compare is constant time over the ASCII bytes of both values. The
  token is never written to a recording, a log, or the recording's
  `meta.config` (§3).

## 3. Recording (Chosen, brief 3.8 / B5) [R1-R4]

- **[R1] Packets only, whole flight.** A recording is one JSONL file per
  mission process: one canonical JSON object per line [C5]. It carries
  packets, the full MAVLink log, and the mission loop's time inputs. It never
  carries image frames or pixels of any kind; no code path in the drone stack
  records frames in flight.
- **[R2] Records.** Every record has `t_rx` and `stream`. For an input record
  `t_rx` is when the companion received it; for an output record it is the
  stamp of the input that caused it [M2].

  | `stream` | Other fields | Content |
  | --- | --- | --- |
  | `meta` | `format` = `skyweave-drone-rec`, `format_v` = 1, `config` (object) | First line. The configuration replay needs: mission constants (including `coast_cap`, `track_timeout_ms`), guidance constants, camera model, link constants, law name, gate setting. Never the token |
  | `detection`, `track`, `mission_state`, `fc_link_health`, `ack` | `pkt` (the packet object) | Packets as on the wire |
  | `command` | `pkt` (the command object; it never contains `token`), `auth_ok` (bool) | Commands as received; the token is removed and replaced by the authentication result. A command record that carries a token is malformed |
  | `mavlink` | `dir` (`rx` or `tx`), `raw` (base64 of the MAVLink2 frame bytes) | Every MAVLink frame the companion receives or sends |
  | `tick` | - | One mission-loop tick (drives timed transitions and setpoint steps) |
  | `ground_hb` | - | One ground-link heartbeat (a UI poll or an authenticated ground command) |

- **[R3] Replay determinism.** The core's notion of now is the stamp of the
  input it is processing; it never reads a clock, and ticks are its only
  source of periodic or timed output. Feeding a recording's input records
  (`track`, `command`, `mavlink` rx, `tick`, `ground_hb`) in file order into a
  fresh companion core built from its `meta.config` reproduces exactly: its
  `mission_state` records; its `ack` records other than `rejected_malformed`
  (those have no input record); and the ordered sequence of velocity
  setpoints in its `mavlink` tx records, compared on (coordinate frame,
  type_mask, vx, vy, vz, yaw_rate) at float32 precision.
- **[R4] Miss vector offline.** The commit miss vector [G6] is computable from
  a recording alone: the `track` record whose `pkt.track_id` and `pkt.t_cap`
  match the `commit:<track_id>:<t_cap>` event; `target_width_m` from the last
  accepted `prime` command record before that event; `f`, `cx`, `cy` from
  `meta.config`. The result equals the logged `miss:` event within its
  rounding.

## 4. Mission state machine (Chosen, brief 3.2-3.4)

### 4.1 States, inputs, evaluation

- **[M1] States** are the 13 values of `mission_state` [P3]. Before the first
  accepted prime the mission is unprimed: it has no state and publishes no
  state packet. Airborne set `A` = {`LAUNCH`, `SEARCH`, `ACQUIRING`,
  `ENGAGED`, `COASTING`, `TOUCH`, `LOST`}.
- **[M2] Inputs.** Each input has a stamp: its record `t_rx` (board ms). Kinds:
  `CMD` (a command with its authentication result), `TRK` (a track packet),
  `TICK` (a mission-loop tick; the vehicle snapshot is taken on ticks only),
  `GDE` (a guidance event: `commit`, `pass_done`, `hold_complete`; stamped
  with the stamp of the input that made guidance emit it, and processed right
  after that input as its own input), and `HB` (a ground heartbeat). `t_cap`
  is guidance geometry only ([G1]); it is never mission time. The mission core
  is pure logic: it never reads a clock, and the same inputs in the same order
  give the same outputs [R3].
- **[M2a] Vehicle predicates** (from the newest MAVLink frames from the FC's
  system and autopilot component):
  - `link up`: `fc_link_up` [P4]. Every other predicate is "unknown" while the
    link is down.
  - `armed`: the newest `HEARTBEAT` has `MAV_MODE_FLAG_SAFETY_ARMED` set;
    `disarmed` when it is clear; unknown before any `HEARTBEAT`.
  - `mode`: the ArduCopter mode name of the newest `HEARTBEAT` `custom_mode`;
    unknown before any.
  - `on_ground`: newest `EXTENDED_SYS_STATE.landed_state` = `ON_GROUND`.
    `airborne`: it is `TAKEOFF`, `IN_AIR`, or `LANDING`. `UNDEFINED` or never
    received is unknown (neither).
  - `rel_alt`: `GLOBAL_POSITION_INT.relative_alt` / 1000 m; `home_dist`: the
    horizontal norm of `LOCAL_POSITION_NED` (x, y); `battery`:
    `SYS_STATUS.battery_remaining` (unknown when -1). Unknown before any.
  - `attitude fresh`: [F3] not degraded.
- **[M3] Sources** (who may trigger): `HUMAN` (UI command, radio approve
  switch, radio mode switch), `TRACKER`, `GUIDANCE`, `VEHICLE` (FC telemetry
  progress), `BUDGET`, `FAILSAFE`, `AUTO` (a follow-on transition with no new
  trigger).
- **[M4] One transition per input.** Each input causes at most one
  transition: among the rows whose input kind and From state match, the one
  ranked highest here:
  T19 > T20 > T18 > T17 > T13 > T06, T10 > T07 > T11, T12, T14 > T08, T09 >
  T05 > T03, T04, T23 > T01, T02, T24 > AUTO (T15, T16, T21, T22).
  When several T17 (or T18) conditions hold at once, the event names the
  first in this order: fc_link, ground_link; flight_time, battery, geofence,
  reacquire. A row's side effects (setting or clearing the candidate or the
  engaged id, requests, tones) happen only when that row fires.
- **[M4a] AUTO rows** fire on the first input after the one that entered their
  From state, and consume it: that input causes the AUTO row or a
  higher-ranked row, never a second transition. A command whose own row does
  not fire is acked `rejected_state`.

### 4.2 Transition table (who may trigger)

Every transition the code makes is a row here; any other change is a defect.

| ID | From | To | Input | Trigger | Source |
| --- | --- | --- | --- | --- | --- |
| T01 | unprimed | `PRIMED` | CMD | `prime` accepted (vehicle disarmed and on_ground) | HUMAN |
| T02 | `PRIMED` | `PRIMED` | CMD | `prime` accepted (disarmed and on_ground); replaces the trial | HUMAN |
| T03 | `PRIMED` | `LAUNCH` | TICK | Link up, mode `GUIDED`, on_ground, disarmed, attitude fresh, AND since the latest accepted prime a snapshot with link up and a known mode other than `GUIDED` was seen (the pilot moved the switch into GUIDED after priming) | HUMAN (radio mode switch, seen through VEHICLE) |
| T04 | `LAUNCH` | `SEARCH` | TICK | airborne and `rel_alt` >= `takeoff_done_frac` x `search_alt` | VEHICLE |
| T05 | `SEARCH` | `ACQUIRING` | TRK | A packet with `state = confirmed`; its `track_id` becomes the candidate | TRACKER |
| T06 | `ACQUIRING` | `SEARCH` | TRK, TICK | Candidate death [P2a] (final packet on TRK, timeout on TICK) | TRACKER |
| T07 | `ACQUIRING` | `ENGAGED` | CMD, any | `approve_engage` accepted [M8]; or, when `engage_preauthorized` was primed, the first input after T05; `engaged_track_id` := candidate | HUMAN |
| T08 | `ENGAGED` | `COASTING` | TRK | Engaged-track packet with `state = coasting` and `misses <= coast_cap` | TRACKER |
| T09 | `COASTING` | `ENGAGED` | TRK | Engaged-track packet with `state = confirmed` | TRACKER |
| T10 | `ENGAGED`, `COASTING` | `LOST` | TRK, TICK | Engaged-track death [P2a]; `engaged_track_id` := null | TRACKER |
| T11 | `ENGAGED` | `TOUCH` | GDE | `commit` (touch trials only) | GUIDANCE |
| T12 | `ENGAGED` | `COMPLETE` | GDE | `hold_complete` (standoff trials only) | GUIDANCE |
| T13 | `SEARCH`, `ACQUIRING`, `ENGAGED`, `COASTING`, `TOUCH`, `LOST` | `COMPLETE` | CMD | `mark_complete` accepted | HUMAN |
| T14 | `TOUCH` | `MISS` | GDE | `pass_done` without a completion mark | GUIDANCE |
| T15 | `LOST` | `SEARCH` | any | AUTO; opens the reacquire window (`reacquire_window_ms`) | AUTO |
| T16 | `COMPLETE`, `MISS` | `RETURN` | any | AUTO (v1: one pass, unconditional RTL) | AUTO |
| T17 | `A` | `RETURN` | TICK | Budget: flight time since LAUNCH > `flight_time_cap_s`; battery < `battery_floor_pct`; `home_dist` > `geofence_radius_m`; or the reacquire window expired before `ENGAGED` | BUDGET |
| T18 | `A` | `RETURN` | TICK | Link loss: FC link down, or no ground heartbeat for `ground_link_timeout_ms` | FAILSAFE |
| T19 | every state | `ABORT` | CMD | `abort` accepted (unprimed: see [M5]) | HUMAN |
| T20 | `A` | `ABORT` | TICK | Link up and a known mode other than `GUIDED` (radio hard abort, or an FC failsafe) | FAILSAFE |
| T21 | `ABORT` | `RETURN` | any | AUTO, vehicle airborne or unknown | AUTO |
| T22 | `ABORT` | `LAND` | any | AUTO, vehicle on_ground | AUTO |
| T23 | `RETURN` | `LAND` | TICK | Link up and landed_state `LANDING` or `ON_GROUND`, or mode `LAND` | VEHICLE |
| T24 | `LAND` | `PRIMED` | CMD | `prime` accepted (disarmed and on_ground); next trial | HUMAN |

- **[M5] Command validity.** `prime`: unprimed, `PRIMED`, or `LAND`, each only
  while the vehicle is disarmed and on_ground. `approve_engage`: `ACQUIRING`
  only, per [M8]. `mark_complete`: the T13 From states. `abort`: every state.
  While unprimed an `abort` is acked `rejected_state`: no state exists to
  abort, and the radio hard abort [M11] is the operator's exit (E1-F6).
  Anything else is `rejected_state` [P5b].
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
  T05, it changes only by its death (T06). `approve_engage` (UI or radio)
  engages the current candidate and is accepted only when that candidate has
  been the candidate for at least `approve_settle_ms` at the approve's stamp;
  otherwise `rejected_state`. The wire cannot name the approved track; the
  settle time narrows the race where a candidate changes between what the
  human saw and the approve (E1-F5).
- **[M9]** Engaged-track death in `TOUCH`, `COMPLETE`, `MISS`, `RETURN`,
  `LAND`, or `ABORT` clears `engaged_track_id` and causes no transition.

### 4.4 Exits (brief 3.3)

- **[M10]** Every exit is an FC-owned mode change, and the companion never
  overrides a mode the pilot or an FC failsafe selected. While in `RETURN`,
  on each tick with link up and mode `GUIDED`, the mission requests `RTL`, at
  most once per `exit_retry_ms` (the first request immediately on entry). The
  first tick with link up and a known mode other than `GUIDED` ends the
  requests for the rest of that `RETURN`. The same rule requests `LAND` while
  in `LAND` entered by T22 with the vehicle armed; it also stops on disarm.
- **[M11]** The Wi-Fi `abort` is a soft abort: a request. The radio mode
  switch is the hard abort and works with the companion dead (FC-owned, not
  companion code).

### 4.5 Effects

- **[M12]** On entering `LAUNCH` the mission requests arm and takeoff to
  `search_alt`. [M10] governs `RTL` and `LAND` requests. On entering a state
  listed in the tone table (§9) it requests that tone. The mission requests
  nothing else from the FC; velocity setpoints come from guidance.

### 4.6 Event vocabulary (Chosen; receivers ignore unknown names)

| Name | When |
| --- | --- |
| `transition:<FROM>-><TO>` | Every state change; `<FROM>` is `UNPRIMED` for T01 |
| `candidate:<id>` | T05 sets the candidate |
| `engaged:<id>` | T07 sets `engaged_track_id` |
| `track_dead:<id>` | Candidate or engaged-track death |
| `commit:<track_id>:<t_cap>` | T11; the committing engaged packet's id and `t_cap` |
| `miss:<x_m>:<y_m>:<z_m>` | Right after `commit:`; [G6] miss (camera right, down) and range, `%.3f`, `-0.000` written `0.000` |
| `pass_done`, `hold_complete` | T14, T12 |
| `cmd:<command>:<result>` | Every command received while a state exists, with its ack result; a true retry [P5a] adds no second event |
| `budget:<flight_time|battery|geofence|reacquire>` | T17 |
| `failsafe:<fc_link|ground_link|mode>` | T18, T20 |
| `fc_request:<RTL|LAND|ARM_AND_TAKEOFF>` | Each FC request [M10], [M12] |
| `tone:<name>` | A tone request |

## 5. Guidance v1 (Chosen; numbers Provisional, brief 3.5-3.6)

- **[G1] De-rotation.** A track's line of sight in NED is
  `los = R_ned_body(att(t_cap)) . R_body_cam . normalize(ray)`, with
  `ray = ((u - cx)/f, (v_px - cy)/f, 1)`. Attitude samples are indexed by
  their board-ms receive time [F2] (`time_boot_ms` is stored, not mapped, in
  v1; E1-F8). If `t_cap` lies between two samples, roll, pitch, and unwrapped
  yaw are interpolated linearly; outside the stored span the nearest sample
  is held; nothing is extrapolated. The packet is attitude-degraded when the
  sample used lies more than `attitude_bound_ms` from `t_cap`, or no sample
  exists.
- **[G1a] Degraded at a step.** Attitude is degraded for a guidance step when
  [F3] holds at the step's stamp, or the packet the step uses is
  attitude-degraded [G1]. [G4], [G7], and [G8] use only this term.
- **[G2] Size range** (brief 3.5). `Z = f * W / w` with `W` the primed
  `target_width_m`; range along the line of sight `r = Z * |ray|`. Valid when
  `w >= w_min_px`. Otherwise the assume-and-bound fallback applies:
  `r = clamp(r_assume, r_min, r_max)`, and the range source is "assumed".
  Onboard scope is bearing plus size range only; no 3D target estimation.
- **[G3] Pure pursuit to the standoff point** (pluggable law; v1 law). The
  target's relative position is `p = r * los` (NED). The standoff point is
  level with the target, `s` short of it horizontally:
  `q = p - s * p_h / |p_h|`, with `p_h = (p_N, p_E, 0)` (if `|p_h|` < 0.01 m,
  `p_h` is the current heading). `s = d_s` in standoff trials;
  `s = s_touch = max(f*W/(1920*alpha) - deadband_m - margin_m,
  W/2 + nose_clearance_m)` in touch trials, so the hold point lies inside the
  range where the fill clause of [G4] can hold and contact happens only
  through [G5]. Command `v = Kp * q`, zero when `|q| <= deadband_m`; then clamp
  `|v| <= v_max` (trial); then rate-limit `|v_k - v_(k-1)| <= a_max * dt`. The
  limiter runs once per setpoint step, applies only to this ENGAGED output,
  takes `dt` from the board-ms stamps of consecutive steps, and takes
  `v_(k-1)` as the last setpoint guidance output (reset to zero on T07, T09,
  and when degradation clears). Yaw: `az = wrap_pi(atan2(los_E, los_N) -
  yaw_now)`, with `yaw_now` the newest `ATTITUDE` yaw; `yaw_rate =
  clamp(K_yaw * az, +-yaw_rate_max)`; `az > 0` is a target right of the nose.
  The law sits behind an interface that takes the de-rotated observation and
  returns a velocity command; the harness ranks laws by scorecard.
- **[G4] Commit gate** (brief 3.4). In `ENGAGED`, touch trials only, on an
  engaged-track packet: commit when `w / 1920 >= alpha` AND
  `hypot(u - cx, v_px - cy) <= beta * 1920` AND `state = confirmed` AND
  `hits >= k` AND attitude not degraded [G1a]. At most one commit per pass.
- **[G5] Terminal segment.** At commit: contact point `c = r * los` (NED,
  relative to the vehicle), closure speed `v_c = v_max` (trial cap). Fly
  `v_c * c / |c|` with yaw rate 0, open loop (no tracker input), for
  `|c| / v_c + t_overrun_s`; then brake (zero velocity, yaw rate 0) for
  `t_brake_s`; then climb at `v_climb` (yaw rate 0) for `t_climb_s`; then
  emit `pass_done`. Phase boundaries are measured from the commit stamp on the
  injected clock; nothing pauses or extends them. The segment bypasses the
  [G3] limiter and ignores attitude degradation (the FC owns acceleration;
  `t_overrun_s` and `t_brake_s` cover it).
- **[G6] Miss vector at commit.** `eps = (u - cx, v_px - cy)` px of the
  committing packet; `miss = Z * eps / f` m (camera right, down), with `Z` from
  [G2]. Emitted as the `miss:` event right after `commit:` (§4.6) when the
  mission accepts the commit; recomputable from the recording [R4]. It is the
  predicted miss of a pass flown along the boresight.
- **[G7] Standoff hold.** In `ENGAGED`, standoff trials: `hold_complete` when
  `|q| <= hold_tol_m` holds without a break for `hold_time_s`, evaluated on
  engaged-track packets. A coasting packet or degraded attitude breaks the
  hold.
- **[G8] Degraded attitude.** In `SEARCH`, `ACQUIRING`, `ENGAGED`, and
  `COASTING`, while degraded [G1a], guidance commands zero velocity and yaw
  rate 0, and the commit gate stays shut.
- **[G9] Lock.** Guidance consumes only packets whose `track_id` equals
  `engaged_track_id` (`ENGAGED`, `COASTING`) or the candidate (`ACQUIRING`,
  yaw only). Every other packet is ignored [M7].
- **[G10] Per-state output.** Every command carries an explicit yaw rate.
  `SEARCH`: `v_N = v_E = 0`, `v_D = clamp(K_alt * (rel_alt - search_alt),
  +-v_alt_max)` (0 if `rel_alt` is unknown), yaw rate `search_yaw_rate`.
  `ACQUIRING`: the same altitude hold, yaw by the [G3] yaw law on the
  candidate. `ENGAGED`: [G3]. `COASTING`: zero velocity, yaw by the [G3] yaw
  law on the coasting prediction. `LOST`: zero velocity, yaw rate 0. `TOUCH`:
  [G5]. `ABORT`, `COMPLETE`, `MISS`, `RETURN`, `LAND`: zero velocity and yaw
  rate 0, only while link up and mode `GUIDED` (so an exit is never fought).
  Unprimed, `PRIMED`, `LAUNCH`: no setpoints.
- **[G11] Cadence.** Guidance produces setpoints once per setpoint step: a
  tick at least `1000 / setpoint_hz` ms after the previous step.

## 6. fc_link v1 (Chosen; numbers Provisional)

- **[F1] SITL-only interlock (phase E1), two parts.** (a) fc_link accepts only
  loopback MAVLink endpoints (`tcp:127.0.0.1:<port>`,
  `udp...:127.0.0.1:<port>`); a serial device or a non-loopback host is
  refused at construction. (b) A loopback endpoint can still bridge a real FC
  (mavlink-router, MAVProxy), so fc_link is receive-only on each connection
  until the FC system has sent `SIMSTATE` (164) or `SIM_STATE` (108), which
  ArduPilot emits only in SITL builds. Until then it writes zero bytes (one
  exception: a companion `HEARTBEAT`, if SITL needs one before it streams on
  that port); every blocked write is counted. The proof resets on reconnect.
  SITL stream rates for that port come from the SITL defaults file. fc_link
  has no parameter-write code path at all.
- **[F2] Attitude subscription.** After the proof, fc_link requests
  `ATTITUDE` at 50 Hz with `MAV_CMD_SET_MESSAGE_INTERVAL`, plus the telemetry
  [M2a] needs (`HEARTBEAT`, `EXTENDED_SYS_STATE`, `GLOBAL_POSITION_INT`,
  `LOCAL_POSITION_NED`, `SYS_STATUS`, `RC_CHANNELS`). Each `ATTITUDE` is stored
  with its board-ms receive time and its FC `time_boot_ms`. At least 1 s of
  samples is kept for [G1].
- **[F3] Staleness.** `attitude_age_ms` = now minus the newest `ATTITUDE`
  receive time. Attitude is degraded when the age exceeds `attitude_bound_ms`
  or no sample exists. `fc_link_up` per [P4].
- **[F4] Health packet** [P4] at about 1 Hz.
- **[F5] Setpoint gate.** The gate is `locked` by default. Only an explicit
  configuration value enables it. While locked, a velocity send, an arm, or a
  takeoff writes zero bytes to the link and is counted as blocked.
- **[F6] Setpoint form.** Every setpoint is `SET_POSITION_TARGET_LOCAL_NED`,
  frame `LOCAL_NED`, `type_mask` 1479 (0x05C7: velocity and yaw rate used;
  position, acceleration, and yaw ignored). No other mask is sent (3527 would
  hand yaw to ArduCopter's auto-yaw). Independent of guidance, every sent
  setpoint is limited: the horizontal vector is scaled down to `v_xy_hard`
  (direction kept), `v_z` is clamped to `+-v_z_hard`, and the yaw rate to
  `+-yaw_rate_hard`.
- **[F7] Radio approve.** The approve channel is read from `RC_CHANNELS`. A
  sample is valid when `chancount >= approve_channel` and the value is within
  [`approve_pwm_valid_min`, `approve_pwm_valid_max`]. The detector starts
  disarmed; a valid sample below `approve_pwm_high` arms it; a valid sample at
  or above it while armed emits one approve and disarms; an invalid sample or
  `rc_seen` false disarms it. So a first sample already high, or a recovery
  from an RC dropout with the switch high, is not an approve. Each approve
  becomes an in-process `approve_engage` command with
  `cmd_id = rc:approve:<t_rx of the sample>`, authenticated by its source; its
  ack is recorded, it is not recorded as a `command` record (replay re-derives
  it from the `mavlink` rx record). fc_link never sends RC overrides.
- **[F8] Tones.** `PLAY_TUNE` with the tune for a tone name from the §9
  table; an unknown name sends nothing.
- **[F9] Exit requests.** `RTL` and `LAND` mode requests are written only
  while the link is up and the newest `HEARTBEAT` mode is `GUIDED`; a mode seen
  before a link loss does not count. Otherwise zero bytes are written and the
  attempt is counted as blocked.
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
- **[U3]** Ack by id [P5a] with a fresh random `cmd_id` per command, and the
  shared token [P5c]. The page never stores the token beyond its own memory.
- **[U4]** The page shows the state banner, engaged track (and the candidate
  awaiting approval), battery, and link health (FC link, attitude age, RC,
  gate state, ground link), plus the trial echo and recent events.
- **[U5]** The page renders from a recorded packet stream: the same render
  function serves live state and a replayed recording.
- **[U6]** Each page state poll is a ground heartbeat [R2].

## 8. Closed-loop harness and scorecard (Chosen)

- **[S0] Loop.** Synthetic target trajectory -> camera projection (noise,
  dropout, injected boresight error) -> detection packets -> tracker ->
  companion core -> fc_link -> ArduCopter 4.7.0 SITL, closed through the SITL
  vehicle's pose. The injected clock reads SITL boot time (ms) from SITL
  messages. Vehicle truth is SITL's `SIM_STATE` (108), read on the harness's
  own SITL connection, never on the companion's; truth feeds only the camera
  projection and the scorer. Every run takes an explicit seed; scored outputs
  carry no wall-clock values. Closed-loop SITL is not bit-reproducible: each
  run's recording must pass [R3] replay exactly, and the gate is run
  `gate_repeats` times, green every time.
- **Tracker.** If tracker v0 source is not recovered, the harness uses a
  reimplementation of the brief §2 spec, labeled Provisional; its v0 numbers
  (1.4 px RMS, zero ghosts) are not inherited and must be re-earned [K1-K4].
- **Scenarios** (each green under its gate seeds [S7]):

  | ID | Scenario | Green when |
  | --- | --- | --- |
  | S1 | Nominal standoff hold, static balloon, standoff trial | `COMPLETE` by [G7]; true hold error p95 <= `hold_tol_m` over the hold; `RETURN` then `LAND` |
  | S2 | Touch pass, static balloon, touch trial | Commit, pass, `MISS` or `COMPLETE`, `RETURN`, `LAND`; p95 miss at the commit plane <= `miss_p95_max_m` over the S2 gate seeds |
  | S3 | Detection dropout, standoff trial | Short dropout (< coast cap): lock retained, same `engaged_track_id`. Long dropout (> coast cap): `LOST`, `SEARCH`, reacquire (T07) before `budget:reacquire` |
  | S4 | Link loss at commit, touch trial | FC-link blackout at commit: `RETURN`, RTL once the link returns, `LAND`; no safety-floor breach |
  | S5 | Late abort, touch trial | `abort` during `TOUCH`: `ABORT`, `RETURN`, FC in `RTL`; abort latency <= `abort_latency_max_ms` |
  | S6 | Target maneuver, standoff trial | Kite that reverses direction, plus a crossing distractor: lock retention >= `lock_retention_min`; zero retargets |

  The scripted human approves through the command path (UI command packets),
  waiting at least `approve_settle_ms` after the candidate appears.
- **[S7] Gate and probe seeds.** Before the first gate run, each scenario's
  gate seeds are fixed by rule: seed_i = the first 4 bytes, big-endian, of
  `sha256("E1-gate:<scenario>:<i>")`, for i < `gate_seed_count` (20 for S2, 3
  for the others). Probe seeds use the namespace `E1-probe`. Development,
  debugging, and any tuning use probe seeds only. Each scorecard names its
  seed set (`gate` or `probe`).
- **[S8] Metric definitions.** All times are recording `t_rx` (SITL clock).
  - Abort latency: from the accepted abort's `command` record to the first FC
    frame that confirms RTL (`COMMAND_ACK` accepted for the RTL request, or a
    `HEARTBEAT` in RTL).
  - Attribution: an engaged-track packet is attributed to the truth object
    whose projected center lies inside its box (`u +- w/2`, `v_px +- h/2`);
    the nearest if several; "none" if no object.
  - Retarget: an engaged-track packet attributed to a truth object other than
    the one attributed at the latest T07; "none" never counts; counted once
    per switch.
  - Lock retention: over detection frames from the first T07 to the first of
    `commit`, `COMPLETE`, `RETURN`, `ABORT`, `LAND`, and only frames where the
    target is inside the camera's field of view: the fraction where the state
    is `ENGAGED` or `COASTING` and the engaged packet is attributed to the
    target.
  - Hold error: true distance from the vehicle to the standoff point over the
    [G7] hold window.
  - Commit-plane miss: the plane through the target at the vehicle's closest
    approach, normal to the terminal velocity; miss is the distance from the
    target center to where the vehicle crosses it. One value per S2 run; p95
    over the S2 gate seeds.
- **Scorecard JSON per run:** scenario, seed and seed set, backend and
  versions, law, the [S8] metrics that apply, safety floors, final state,
  transition list, [R3] replay result, pass/fail per check.
- **Safety floors:** no geofence breach (true horizontal distance from home
  <= `geofence_radius_m`); true altitude >= `alt_floor_m` while in `SEARCH`
  through `LOST` (`A` minus `LAUNCH`); no sent setpoint faster than `v_max`.

## 9. Provisional defaults

From brief §6 unless marked "E1". All Provisional.

| Name | Value | Where |
| --- | --- | --- |
| `trial_type` (form default) | `standoff` (E1: the non-contact trial is the default) | prime |
| `v_max` | 2.5 m/s (touch trials) | prime |
| `d_s` | 5 m | prime |
| `hold_tol_m`, `hold_time_s` | 1.0 m, 10 s | [G7] |
| `alpha`, `beta`, `k` | 0.4, 0.1, 5 | prime |
| tracker M of N, `coast_cap` | 3 of 5, 20 frames | [K2], [P2a] |
| `pass_budget` | 1 | prime |
| `search_yaw_rate` | 45 deg/s | [G10] |
| `reacquire_window_ms` | 10000 | T15, T17 |
| attitude rate, `attitude_bound_ms`, `setpoint_hz` | 50 Hz, 100 ms, 10 Hz | [F2], [F3], [G11] |
| `search_alt` | 10 m (E1) | prime |
| `target_width_m` | 1.0 m (E1) | prime |
| `engage_preauthorized` | false (E1) | prime |
| `flight_time_cap_s`, `battery_floor_pct`, `geofence_radius_m` | 180 s, 30 %, 60 m (E1) | prime |
| `v_max_hard` | 5.0 m/s (E1) | prime validation |
| `track_timeout_ms` | 500 (E1) | [P2a] |
| `fc_link_bound_ms` | 1000 (E1) | [P4] |
| `ground_link_timeout_ms` | 5000 (E1) | T18 |
| `takeoff_done_frac` | 0.9 (E1) | T04 |
| `approve_settle_ms` | 1000 (E1) | [M8] |
| `exit_retry_ms` | 1000 (E1) | [M10] |
| `tick_hz` | 20 (E1) | [R3], [G11] |
| camera `f`, `cx`, `cy` | 1000 px, 959.5, 599.5 (E1; AR0234 lens uncalibrated) | [G1] |
| `w_min_px`, `r_assume`, `r_min`, `r_max` | 4 px, 15 m, 1 m, 60 m (E1) | [G2] |
| `Kp`, `deadband_m`, `a_max` | 0.8 1/s, 0.25 m, 2.0 m/s^2 (E1) | [G3] |
| `margin_m`, `nose_clearance_m` | 0.05 m, 0.3 m (E1) | [G3] |
| `K_yaw`, `yaw_rate_max` | 1.5 1/s, 90 deg/s (E1) | [G3] |
| `K_alt`, `v_alt_max` | 0.5 1/s, 1.0 m/s (E1) | [G10] |
| `t_overrun_s`, `t_brake_s`, `v_climb`, `t_climb_s` | 0.5 s, 1.0 s, 1.5 m/s, 2.0 s (E1) | [G5] |
| `v_xy_hard`, `v_z_hard`, `yaw_rate_hard` | 5.0 m/s, 2.0 m/s, 90 deg/s (E1) | [F6] |
| `approve_channel`, `approve_pwm_high` | RC 8, 1700 us (E1) | [F7] |
| `approve_pwm_valid_min`, `approve_pwm_valid_max` | 800 us, 2200 us (E1) | [F7] |
| `mission_publish_hz` | 5 Hz on ticks, plus one packet after each input that changes state (E1) | [P3] |
| UDP ports | detection 14601, track 14602, mission_state 14603, fc_link_health 14604, command 14605 (E1) | [C8], [C10] |
| `miss_p95_max_m` | 0.5 m (E1, declared before the first run) | S2 |
| `abort_latency_max_ms` | 500 (E1, declared before the first run) | S5 |
| `lock_retention_min` | 0.95 (E1, declared before the first run) | S6 |
| `alt_floor_m` | 2.0 m (E1) | safety floor |
| `gate_seed_count` | 20 for S2, 3 otherwise (E1) | [S7] |
| `gate_repeats` | 2 (E1) | [S0] |

Tone table (E1, [F8], [M12]): `ACQUIRING`, `ENGAGED`, `TOUCH`, `LOST`,
`RETURN`, `ABORT` each have a distinct short tune; other states have none.

## 10. Done when (tests)

| Series | Clauses | Content | Tier |
| --- | --- | --- | --- |
| P | [C5]-[C9], [P1]-[P5] | Golden packet fixtures beside the tests; byte-exact canonical encoding; decode/encode round-trip; unknown-field tolerance; version, type, range, and undecodable-input rejection through one error type | fast |
| R | [R1]-[R4] | Recording round-trip; token never recorded; replay of a recorded stream reproduces mission states, acks (except `rejected_malformed`), and setpoints; miss vector recomputed offline | fast |
| M | [M1]-[M12], T01-T24 | Every transition and every command rejection; precedence; lock stickiness against a higher-scoring track; approve settle; edge-gated launch; exit retries | fast |
| K | [K1]-[K4], [P2a] | Provisional tracker: M-of-N confirm, coast cap and final packet, id uniqueness across restarts | fast |
| G | [G1]-[G11] | Synthetic geometry: known offsets give known commands, commit decisions, and miss vectors | fast |
| F | [F1]-[F11] | Interlock refusal and SITL proof, gate provably blocks sends, setpoint form and limits, staleness via injected clock, radio approve detector; SITL scripted run (arm, takeoff, velocity, land) and injected attitude staleness | fast + slow (SITL) |
| U | [U1]-[U6] | Headless: command idempotency, invalid-state rejection, four commands only, render from a recorded stream | fast |
| S | [S0]-[S8] | Closed-loop SITL scenarios under gate seeds, `gate_repeats` times, [R3] per run, scorecard emitted | slow (SITL) |

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
| E1-D2 | Deferred with triggers: binary packets (measured serialization cost), Mahalanobis association (recorded IoU failure in replay; an S6 retarget by id swap is such evidence), IMM (recorded CV-model failure on a real maneuver) | Deferred |
| E1-D3 | Standoff point level with the target ([G3]), and the touch-trial standoff `s_touch`. Reason: brief §6 defaults make a 5 m standoff incompatible with the commit gate whenever `f*W < alpha*1920*d_s` (3840 px*m; for W = 1 m, any lens wider than about 28 deg HFOV), and a pursuit along a fixed line of sight never centers a target above the horizon vertically. Needs Samuel's ratification | Provisional |
| E1-D4 | T03 is edge-gated: a UI prime never launches by itself; the pilot moves the switch into GUIDED after priming (brief 3.10 hazard, 3.11 engage gate) | Chosen |
| E1-D5 | `cmd_id` unique per mission process across senders, fingerprinted retries, `rejected_duplicate_id` (a reloaded page can no longer reuse an id and have an abort swallowed) | Chosen |
| E1-D6 | Exit requests repeat while GUIDED ([M10]); zero setpoints in exit states while GUIDED ([G10]) | Chosen |
| E1-D7 | fc_link two-part interlock: loopback plus SITL proof ([F1]) | Chosen |
| E1-D8 | Time model: input stamps, one transition per input, ticks as the only timed source ([M2], [M4], [R3]) | Chosen |
| E1-D9 | Setpoint `type_mask` 1479 with an explicit yaw rate always ([F6], [G10]) | Chosen |
| E1-D10 | SEARCH holds the primed search altitude ([G10], brief §6) | Chosen |
| E1-F1 | Box origin (top-left vs center) and `t_cap` source of the deployed percepd v0 are unverified. Check against a v0 capture on the Cubie before percepd v1; percepd v1 converts at its encoder if needed | Finding |
| E1-F2 | percepd v0 and tracker v0 emit no `v` field; their packets are not v1 packets. percepd v1 adds it (brief work item 7) | Finding |
| E1-F3 | AR0234 lens intrinsics are uncalibrated; `f`, `cx`, `cy` are placeholders. Boresight calibration from logged miss vectors is an open question (working doc §15) | Finding |
| E1-F4 | The final track packet is not self-identifying; death detection depends on `coast_cap` being shared. Remedy if a mismatch shows up: an additive `final: bool` track field (Samuel decides; it extends the brief §4 field list) | Finding |
| E1-F5 | The approve race is narrowed by `approve_settle_ms`, not closed: the wire cannot name the approved candidate. Remedy: an additive `params.track_id` on `approve_engage` (Samuel decides; it extends the brief §4 field list) | Finding |
| E1-F6 | A companion-process restart while airborne leaves the mission unprimed: prime is refused (not on the ground), the Wi-Fi abort is `rejected_state`, companion budgets and failsafes are off, and guidance sends nothing. The radio hard abort and the FC failsafes are the exits. Not built for in E1 | Finding |
| E1-F7 | An RTL refused while GUIDED gives a zero-velocity hover with RTL requested every `exit_retry_ms`; the pilot takes over by radio. No escalation in E1 | Finding |
| E1-F8 | Attitude receive time includes serial and scheduling latency; v1 does not model it or map `time_boot_ms` | Finding |
| E1-F9 | Camera mount roll and uptilt are assumed zero ([C3]); verify on the airframe | Finding |
| E1-F10 | Live detection recording needs percepd to also send detections to the companion; percepd v0 and tracker v0 likely do not. Deferred to percepd v1 (work item 7). The harness records detections in process | Finding |
| E1-F11 | `LAUNCH` has no own timeout; a refused arm leaves it in `LAUNCH` until the flight-time budget | Finding |
| E1-F12 | The commit gate can stay shut at the touch hold point (fill unreachable for the primed `W` and lens, a clipped box, persistent offset); the vehicle then holds until `mark_complete`, abort, or budget | Finding |
| E1-F13 | The candidate choice among several confirmed tracks depends on the tracker's emission order | Finding |
| E1-F14 | The UI token travels in clear over the Cubie Wi-Fi AP; acceptable for controlled demos only | Finding |
