# SkyWeave drone stack contracts (E1 / D0)

**Revision:** 2026-10-08, amended 2026-11-20 (§11.1 vetting decisions; §8.1
probe scenarios for V-F3, V-D15, V-D16, V-F2); 2026-11-21 (§11.2 E2
planning decisions; E2a-1 and E2a-2: [C2], [C3], [G1], [G2], [G4], [G6],
[R2], [R4] and §9 for V-D1, E2-D2 and V-D11); 2026-11-24 (§11.3 E1
leftover decisions; [P5], [M5], [M8], [G5], [G7], [G10], [G12], [U3], [U6], [U7]; amended
2026-11-27: E1-D15 keeps the token across a reload)
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
  640 x 360 sprint mode is reserved; it is not representable in v1. Packet
  pixels are raw sensor pixels, distorted by the lens, as in detection D0.
  Guidance undistorts a box before it uses it ([G1], V-D11).
- **[C3] Frames.** Vehicle body FRD (x forward, y right, z down). Local level
  frame NED, origin at the FC's EKF origin (home). Camera: OpenCV (+X right,
  +Y down, +Z along the optical axis). `R_A_B` maps vectors from frame B into
  frame A. Attitude is ArduPilot `ATTITUDE` roll, pitch, yaw (radians), with
  `R_ned_body = Rz(yaw) Ry(pitch) Rx(roll)`. The camera mount `R_body_cam` is
  configured (V-D1): a named mount or a measured one, recorded in
  `meta.config` [R2] with the lens (§9). Named mounts:
  - `c3_forward`, upright with no uptilt: `R_body_cam = [[0,0,1],[1,0,0],[0,1,0]]`
    (body x = camera Z, body y = camera X, body z = camera Y). The E1 mount.
    The E1 gate and probes P1-P7 keep it (E2-D1).
  - `cone_up` (E2-D2): `R_body_cam = [[1,0,0],[0,-1,0],[0,0,-1]]`. The
    optical axis lies along body -z (up at hover), image u along body x,
    image v along body -y.
- **[C4] Units.** Meters, m/s, seconds, radians inside code. Packet fields carry
  the unit named in their table. `alpha` and `beta` are fractions of frame
  width (1920 px). Positive yaw rate turns the nose right (clockwise seen
  from above), as in MAVLink `LOCAL_NED`.
- **[C5] Encoding.** UTF-8 JSON, one packet per UDP datagram, one JSON object
  per packet. The canonical encoder writes ASCII only (non-ASCII escaped),
  sorted keys, separators `,` and `:`, no whitespace, finite numbers only (no
  NaN, Infinity, or a literal that overflows to infinity). A packet nests at
  most 32 levels deep; deeper input is refused before parsing. Receivers must
  not depend on key order or whitespace.
- **[C6] Types.** An `int` field accepts only JSON integers (`1`, never `1.0`
  and never `true`), within the JSON-safe range +-(2^53 - 1). A `float` field
  accepts JSON integers or reals, never booleans. A `bool` field accepts only
  `true`/`false`. A `string` enum field accepts only the listed values.
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
- **[C10] Process topology.** The board runs two processes: percepd and the
  companion (E2-D5; the tracker was a third process before). The companion
  process holds the tracker, mission, guidance, fc_link, the UI server, and
  the recorder; hand-offs inside it are in-process calls, not datagrams.
  "Mission process" in this document means the companion process. The
  pure-logic part of it (the tracker plus mission plus guidance plus the
  vehicle-state parser) is the companion core of [R3]. Each UDP port has
  exactly one bound receiver; `SO_REUSEPORT` fan-out is not used. detection
  and command go to the companion; the companion publishes track,
  mission_state and fc_link_health on their ports for external listeners
  (E2-D5). The packets do not change: the tracker inside the companion makes
  each [P2] track packet, feeds it to the mission, and publishes it.

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
| `params` | object | - | Required for `prime` (prime params, below). Optional for `approve_engage` (approve params, below). Ignored for the other two |

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

approve params (E1-D12; additive, so a v1 sender that sends none still
works):

| Field | Type | Unit | Meaning |
| --- | --- | --- | --- |
| `track_id` | int, optional | - | The `track_id` of the candidate the sender saw. Checked by [M8]. A value that is not an int is acked `rejected_params` |

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
  | `meta` | `format` = `skyweave-drone-rec`, `format_v` = 1, `config` (object) | First line. The configuration replay needs: mission constants (including `coast_cap`, `track_timeout_ms`), guidance constants, camera model (mount and lens names, `R_body_cam`, `f`, `cx`, `cy`, distortion; V-D1, V-D11), link constants, law name, gate setting, and, when the companion runs the tracker (E2-D5), the tracker constants (key `tracker`) with its `id_base` [K4]. Never the token. A camera object without the names and distortion (an E1 recording) reads as the E1 camera: `c3_forward`, `e1_f1000`, no distortion |
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
  type_mask, vx, vy, vz, yaw_rate) at float32 precision. When `meta.config`
  carries the tracker (E2-D5), the core runs it: `detection` records are
  inputs in place of `track` records, and replay also reproduces the `track`
  records exactly (every field, with their `t_rx`, in file order). A
  recording without the tracker key replays as before, from its `track`
  records.
- **[R4] Miss vector offline.** The commit miss vector [G6] is computable from
  a recording alone: the `track` record whose `pkt.track_id` and `pkt.t_cap`
  match the `commit:<track_id>:<t_cap>` event; `target_width_m` from the last
  accepted `prime` command record before that event; the camera (`f`, `cx`,
  `cy` and the distortion, V-D11) from `meta.config`. The result equals the
  logged `miss:` event within its rounding.

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
  only, per [M8]; in `ACQUIRING`, approve params of the wrong type are
  `rejected_params`. `mark_complete`: the T13 From states. `abort`: every state.
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
  otherwise `rejected_state`. An approve may name the track it means in
  `params.track_id` [P5]. When it does, it is accepted only if that id equals
  the current candidate's `track_id` (and the settle time holds); a different
  id is `rejected_state`, and nothing is engaged. This closes the race where
  the candidate changes between what the human saw and the approve (E1-F5,
  E1-D12). An approve with no `track_id` engages the current candidate as
  before; the radio approve [F7] is such an approve.
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
  - **Undistorted box (V-D11).** In [G1], [G2], [G4] and [G6], `u`, `v_px`
    and `w` are the track box undistorted into the pinhole image. The model
    is Brown-Conrady `(k1, k2, p1, p2, k3)` on normalized coordinates, the
    detection D0 convention (`contracts/camera.py`); the coefficients are
    camera config, zero until calibrated (§9). The box center is
    undistorted. The width is the pinhole length whose distorted end points,
    on the row through that center, lie the raw width apart; the height
    likewise, on the column. The synthetic camera applies the same rule
    forward. With zero coefficients the box is the packet's. A box past the
    model's domain (the radius where the radial term stops growing) has no
    undistorted form; its ray is not finite, so the packet is
    attitude-degraded ([G1a]).
- **[G1a] Degraded at a step.** Attitude is degraded for a guidance step when
  [F3] holds at the step's stamp, or the packet the step uses is
  attitude-degraded [G1]. A packet whose ray or [G2] range is not finite and
  positive counts as attitude-degraded too. [G4], [G7], and [G8] use only this
  term.
- **[G2] Size range** (brief 3.5). `Z = f * W / w` with `W` the primed
  `target_width_m` and `w` undistorted ([G1], V-D11); range along the line of
  sight `r = Z * |ray|`. Valid when
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
  `hits >= k` AND attitude not degraded [G1a]. `u`, `v_px` and `w` are of the
  undistorted box ([G1], V-D11). At most one commit per pass.
- **[G5] Terminal segment.** At commit: contact point `c = r * los` (NED,
  relative to the vehicle), closure speed `v_c = v_max` (trial cap). Fly
  `v_c * c / |c|` with yaw rate 0, open loop (no tracker input), for
  `|c| / v_c + t_overrun_s`; then brake (zero velocity, yaw rate 0) for
  `t_brake_s`; then climb at `min(v_climb, v_max)` [G12] (yaw rate 0) for
  `t_climb_s`; then
  emit `pass_done`. Phase boundaries are measured from the commit stamp on the
  injected clock; nothing pauses or extends them. The segment bypasses the
  [G3] limiter and ignores attitude degradation (the FC owns acceleration;
  `t_overrun_s` and `t_brake_s` cover it).
- **[G6] Miss vector at commit.** `eps = (u - cx, v_px - cy)` px of the
  committing packet's undistorted center ([G1], V-D11); `miss = Z * eps / f`
  m (camera right, down), with `Z` from
  [G2]. Emitted as the `miss:` event right after `commit:` (§4.6) when the
  mission accepts the commit; recomputable from the recording [R4]. It is the
  predicted miss of a pass flown along the boresight.
- **[G7] Standoff hold** (E1-D14). Standoff trials. The hold clock starts at
  an engaged-track packet in `ENGAGED` with `|q| <= hold_tol_m`. It keeps
  running while the engaged track coasts (state `COASTING`, at most
  `coast_cap` frames [P2a]). It stops, and a later hold starts from zero,
  when an engaged-track packet in `ENGAGED` has `|q| > hold_tol_m` (or `q`
  not finite), when attitude is degraded [G1a], or when the state is neither
  `ENGAGED` nor `COASTING`. `hold_complete` is emitted on the first
  engaged-track packet in `ENGAGED` (never a coasting one) at which the clock
  has run `hold_time_s`.
- **[G8] Degraded attitude.** In `SEARCH`, `ACQUIRING`, `ENGAGED`, and
  `COASTING`, while degraded [G1a], guidance commands zero velocity and yaw
  rate 0, and the commit gate stays shut.
- **[G9] Lock.** Guidance consumes only packets whose `track_id` equals
  `engaged_track_id` (`ENGAGED`, `COASTING`) or the candidate (`ACQUIRING`,
  yaw only). Every other packet is ignored [M7].
- **[G10] Per-state output.** Every command carries an explicit yaw rate.
  `SEARCH`: `v_N = v_E = 0`, `v_D = clamp(K_alt * (rel_alt - search_alt),
  +-min(v_alt_max, v_max))` [G12] (0 if `rel_alt` is unknown), yaw rate
  `search_yaw_rate`.
  `ACQUIRING`: the same altitude hold, yaw by the [G3] yaw law on the
  candidate. `ENGAGED`: [G3]. `COASTING`: zero velocity, yaw by the [G3] yaw
  law on the coasting prediction. `LOST`: zero velocity, yaw rate 0. `TOUCH`:
  [G5]. `ABORT`, `COMPLETE`, `MISS`, `RETURN`, `LAND`: zero velocity and yaw
  rate 0, only while link up and mode `GUIDED` (so an exit is never fought).
  Unprimed, `PRIMED`, `LAUNCH`: no setpoints.
- **[G11] Cadence.** Guidance produces setpoints once per setpoint step: a
  tick at least `1000 / setpoint_hz` ms after the previous step.
- **[G12] Trial speed cap** (E1-D13). No velocity command guidance outputs is
  faster than the trial's `v_max`, in any direction (3D norm). The altitude
  hold [G10] and the terminal climb [G5] obey it like the pursuit [G3]. So
  the [S8] floor "no sent setpoint faster than `v_max`" holds for every
  valid prime. The E2 acceleration setpoints are outside this clause
  (E2-D4).

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
  shared token [P5c]. The page keeps the token in its own memory and in the
  tab's `sessionStorage`, so a reload of the same tab keeps it (E1-D15). It
  never writes the token to `localStorage`, a cookie, or a URL. Closing the
  tab drops it.
- **[U4]** The page shows the state banner, engaged track (and the candidate
  awaiting approval), battery, and link health (FC link, attitude age, RC,
  gate state, ground link), plus the trial echo and recent events.
- **[U5]** The page renders from a recorded packet stream: the same render
  function serves live state and a replayed recording.
- **[U6]** (E1-D15) A page state poll is a ground heartbeat [R2] only when
  it carries the shared token in the `X-Skyweave-Token` request header and
  the token matches ([P5c] compare). The page sends the header once the
  operator has typed the token. `GET /state` answers every poll with the
  view; a poll with no token or a wrong one is not a heartbeat, writes no
  `ground_hb` record, and is counted. The token never travels in a URL. A
  reload of the same tab keeps the token [U3], so the heartbeat resumes with
  the first poll after the reload. A new tab, a closed tab, or a browser
  restart has no token: the heartbeat stops until the operator types it
  again, and in flight T18 fires after `ground_link_timeout_ms`.
- **[U7]** (E1-D12) The page sends `approve_engage` only with
  `params.track_id` set to the candidate it displays. With no candidate
  displayed it sends nothing.

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

### 8.1 Probe scenarios, wind, bad frames, airframe (2026-11-20)

These clauses implement §11.1 decisions V-F3, V-D15, V-D16 and V-F2. They
add probe scenarios beside the gate. They change no gate row above, no [S7]
gate seed, no §9 number, and no guidance or tracker clause. Every number in
a probe world is Provisional.

- **[S9] Probe scenarios (V-F3).** The scenarios below are not gate scenes.
  They have no gate seeds. Their probe seeds are seed_i = the first 4 bytes,
  big-endian, of `sha256("V-probe:<scenario>:<i>")`. A seed equal to any
  S1-S6 gate seed is refused. A probe run is scored against the table rows
  in its "Scored as" column: the union of those rows' checks, plus the
  safety floors, [R3] replay, and run completion. Its scorecard names those
  rows (`checks_as`) and carries its world. A probe scenario never counts
  toward the gate verdict ([S0], `gate_complete`). Failing probes are
  expected until the guidance and tracking decisions (V-D2 to V-D12) land;
  they are a baseline, not a gate.

  | ID | Probe | Trial | Scored as |
  | --- | --- | --- | --- |
  | P1 | Static balloon 20-60 deg above the vehicle | touch | S2 |
  | P2 | Tethered balloon blown and swaying in the wind (V-D14), SITL wind | touch | S2 |
  | P3 | Kite flying figure-8s in the wind, SITL wind | standoff | S6 |
  | P4 | Thrown paper plane through the yaw scan, engage preauthorized | touch | S2, S6 |
  | P5 | False-box clutter and a persistent circling bird | standoff | S1, S6 |
  | P6 | Detector misses, frame drops, burst drops, latency jitter | touch | S2 |
  | P7 | The cone-tower X airframe (V-F2, V-F6) | touch | S2 |

- **[S10] Wind (V-D15).** A probe world may carry a wind: a steady speed
  and direction plus SITL turbulence. It reaches SITL only through the
  launch defaults file: `SIM_WIND_SPD`, `SIM_WIND_DIR` (where the wind comes
  from, deg true), `SIM_WIND_TURB`, `SIM_WIND_TC`, and `SIM_WIND_T` = 1 (no
  height scaling). The targets that drift in the wind (P2, P3) use the same
  speed and direction. No code path writes a parameter to a running FC
  ([F1]). Gate worlds carry no wind.
- **[S11] Bad frames (V-D16).** The synthetic camera may add, per world:
  clutter boxes that match no truth object; whole-frame drops, random and in
  bursts (a dropped frame produces no detection packet); and latency
  jitter (delivery stays in frame order). A persistent false track is a
  scene object that the detector reports as a target (P5's bird). Every
  feature is off by default. With all of them off, the camera's output is
  identical to E1's, frame by frame. Gate worlds use none. Random dropout
  in a standoff hold still meets E1-F16.
- **[S12] Airframe (V-F2).** A world names the airframe SITL flies: `plus`
  (SITL model `+`, the E1 gate airframe) or `cone_x` (the V-F6 X quad, from
  the JSON frame file `harness/frames/cone_x.json` with `FRAME_TYPE` 1 and
  the operator's hover throttle and 6S battery limits in the defaults file).
  Gate scenes S1-S6 fly `plus`. The frame file's numbers and sources are in
  its README; all are Provisional.

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
| camera mount (V-D1) | `c3_forward` (E1; the E1 gate and probes P1-P7) or `cone_up` (E2-D2; E2b and the E2 gate). Both nominal, not measured (E1-F9) | [C3] |
| camera lens `f`, `cx`, `cy` (V-D1) | `e1_f1000`: 1000 px, 959.5, 599.5 (E1 placeholder). `hfov100`: 806 px, 959.5, 599.5 (100 deg HFOV, `960 / tan(50 deg)` = 805.5; half-angles 50.0 deg along u, 36.7 deg along v; V-F1, E2-D2). Both uncalibrated (E1-F3) | [G1] |
| lens distortion `(k1, k2, p1, p2, k3)` (V-D11) | all 0 until calibrated. The E2a-2 closed-loop check uses `k1` = -0.3 as a placeholder | [G1] |
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
| R | [R1]-[R4] | Recording round-trip; token never recorded; replay of a recorded stream reproduces mission states, acks (except `rejected_malformed`), and setpoints, and, when the tracker runs in the core, the track records (E2-D5); miss vector recomputed offline | fast |
| M | [M1]-[M12], T01-T24 | Every transition and every command rejection; precedence; lock stickiness against a higher-scoring track; approve settle; edge-gated launch; exit retries | fast |
| K | [K1]-[K4], [P2a] | Provisional tracker: M-of-N confirm, coast cap and final packet, id uniqueness across restarts | fast |
| G | [G1]-[G11] | Synthetic geometry: known offsets give known commands, commit decisions, and miss vectors | fast |
| F | [F1]-[F11] | Interlock refusal and SITL proof, gate provably blocks sends, setpoint form and limits, staleness via injected clock, radio approve detector; SITL scripted run (arm, takeoff, velocity, land) and injected attitude staleness | fast + slow (SITL) |
| U | [U1]-[U6] | Headless: command idempotency, invalid-state rejection, four commands only, render from a recorded stream | fast |
| S | [S0]-[S8] | Closed-loop SITL scenarios under gate seeds, `gate_repeats` times, [R3] per run, scorecard emitted | slow (SITL) |
| S (probe) | [S9]-[S12] | Probe worlds and seeds, wind and airframe through the defaults file, bad frames (gate camera output unchanged), probe scoring; the cone frame hovers and the wind pushes, on SITL | fast + slow (SITL) |

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
| E1-D11 | Robustness from the implementation bug hunt: JSON nesting capped at 32 ([C5]) and integers at +-(2^53 - 1) ([C6]), so every accepted packet re-parses from a recording and every derived event name fits 128 chars; more than 64 pending events force a publish ([C9]); a non-finite packet geometry is a degraded packet ([G1a]); one bad input never ends the companion loop (logged and counted) | Chosen |
| E1-F1 | Box origin (top-left vs center) and `t_cap` source of the deployed percepd v0 are unverified. Check against a v0 capture on the Cubie before percepd v1; percepd v1 converts at its encoder if needed | Finding |
| E1-F2 | percepd v0 and tracker v0 emit no `v` field; their packets are not v1 packets. percepd v1 adds it (brief work item 7) | Finding |
| E1-F3 | AR0234 lens intrinsics are uncalibrated; `f`, `cx`, `cy` are placeholders. Boresight calibration from logged miss vectors is an open question (working doc §15) | Finding |
| E1-F4 | The final track packet is not self-identifying; death detection depends on `coast_cap` being shared. Remedy if a mismatch shows up: an additive `final: bool` track field (Samuel decides; it extends the brief §4 field list) | Finding |
| E1-F5 | The approve race is narrowed by `approve_settle_ms`, not closed: the wire cannot name the approved candidate. Remedy: an additive `params.track_id` on `approve_engage` (Samuel decides; it extends the brief §4 field list). Closed 2026-11-24 by E1-D12 (§11.3) | Finding |
| E1-F6 | A companion-process restart while airborne leaves the mission unprimed: prime is refused (not on the ground), the Wi-Fi abort is `rejected_state`, companion budgets and failsafes are off, and guidance sends nothing. The radio hard abort and the FC failsafes are the exits. Not built for in E1 | Finding |
| E1-F7 | An RTL refused while GUIDED gives a zero-velocity hover with RTL requested every `exit_retry_ms`; the pilot takes over by radio. No escalation in E1 | Finding |
| E1-F8 | Attitude receive time includes serial and scheduling latency; v1 does not model it or map `time_boot_ms` | Finding |
| E1-F9 | Camera mount roll and uptilt are assumed zero ([C3]); verify on the airframe | Finding |
| E1-F10 | Live detection recording needs percepd to also send detections to the companion; percepd v0 and tracker v0 likely do not. Deferred to percepd v1 (work item 7). The harness records detections in process | Finding |
| E1-F11 | `LAUNCH` has no own timeout; a refused arm leaves it in `LAUNCH` until the flight-time budget | Finding |
| E1-F12 | The commit gate can stay shut at the touch hold point (fill unreachable for the primed `W` and lens, a clipped box, persistent offset); the vehicle then holds until `mark_complete`, abort, or budget | Finding |
| E1-F13 | The candidate choice among several confirmed tracks depends on the tracker's emission order | Finding |
| E1-F14 | The UI token travels in clear over the Cubie Wi-Fi AP; acceptable for controlled demos only | Finding |
| E1-F15 | The [S8] floor "no sent setpoint faster than `v_max`" conflicts with [G5] (climb at `v_climb` = 1.5 m/s) and [G10] (altitude hold up to `v_alt_max` = 1.0 m/s) when a trial is primed below those speeds. The E1 gate primes 2.5 m/s, so it does not trigger. Options for Samuel: cap climb and altitude hold at `v_max`; reject such primes; or scope the floor to closure (horizontal) speed, as brief 3.4 caps closure speed. Not changed in E1. Closed 2026-11-24 by E1-D13 (§11.3) | Finding |
| E1-F16 | [G7] restarts the hold on any coasting packet, so a 10 s hold at 60 fps needs about 600 consecutive hits. With 2 % random detection dropout that is about (0.98)^600, 5.5e-6: S1 cannot pass. The E1 gate world therefore has no random dropout (declared before the first run); dropout is scripted in S3 only. Options for Samuel: let the hold tolerate coasting up to a count, or judge it on elapsed time inside tolerance. Closed 2026-11-24 by E1-D14 (§11.3) | Finding |
| E1-F17 | `GET /state` needs no token, yet each poll is a ground heartbeat ([U6]). Any device on the AP that polls keeps the T18 ground-link failsafe from firing. Remedy if wanted: count only token-bearing polls (a [U6] change). Closed 2026-11-24 by E1-D15 (§11.3) | Finding |
| E1-F18 | The core writes `ground_hb` records only for UI polls. An authenticated ground command is a heartbeat inside the mission but leaves no `ground_hb` record (its `command` record drives replay, so [R3] holds). A reader of `ground_hb` alone sees false gaps | Finding |
| E1-F19 | [R3] replay models one SITL proof per recording. A live FC-link close and reconnect (companiond's reconnect path) resets the proof, so the setpoint comparison would diverge. The harness keeps the link open through a relay. Remedy: link connect/disconnect markers in §3 (a decision) | Finding |
| E1-F20 | In the live companion a `mavlink` tx record is stamped when it is written, not with the causing input's stamp ([R2]). The two differ by processing time; in the harness they are equal | Finding |
| E1-F21 | S4's blackout drops bytes both ways with the link open and starts at the commit stamp, so no terminal setpoint is sent. The riskier variant (blackout one setpoint after commit: ArduCopter keeps the last setpoint until `GUID_TIMEOUT`, 3 s, about 7.5 m at 2.5 m/s) is not tested | Finding |
| E1-F22 | `SIM_STATE` (108) has no time field; the harness stamps each sample with the `time_boot_ms` of the `ATTITUDE` sent just before it (strict alternation verified on ArduCopter 4.7.0). In 4.7.0 its float lat/lon fields carry degE7 values; only `lat_int`/`lon_int` are used | Finding |
| E1-F23 | Brief 3.11 names the switch positions MANUAL / GUIDED / RTL; ArduCopter has no MANUAL mode. The SITL harness uses STABILIZE for that position. Closed 2026-11-24 by E1-D16 (§11.3) | Finding |
| E1-F24 | The [F7] radio approve path is covered by fast tests only; the closed-loop human approves through UI command packets (§8) | Finding |
| E1-F25 | `POST /command` does not require a JSON content type, so a page in the operator's browser can post cross-site. The token still gates execution; unauthenticated posts are acked `rejected_auth` and logged | Finding |
| E1-F26 | Radio approves are stamped at their RC sample, so each live driver must stamp its next core input no earlier than the frames fc_link already ingested (companiond, the UI, and the harness do) | Finding |

### 11.1 Vetting review decisions (2026-11-20)

Samuel reviewed the merged E1 stack with Claude on 2026-11-20 and made the
decisions below. They authorize clause changes in the next phase. The
clauses above stay as written until the PR that implements a decision edits
them and cites its `V-` ID. Inside wire v1 every packet change is additive
([C7], §2). Numbers here are Provisional; none is Measured. [F1] (SITL
only, no parameter writes from fc_link) still holds until a phase brief
lifts it.

| ID | Item | Label |
| --- | --- | --- |
| V-F1 | Airframe correction. The vehicle is an X quadrotor with a tall cone tower above the props that holds the battery and electronics, so it is top-heavy. The camera sits at the tip of the nose. At hover the nose points up, along the thrust axis. [C3] (camera Z on body x, no uptilt), the `camera_sim` mount, and the July CAD sheet's forward-camera quad do not describe this vehicle. Lens: 100 deg HFOV, so `f` is about 806 px on 1920 px (half-angles about 50 deg wide and 36.7 deg short). It replaces the 1000 px placeholder and is still uncalibrated (E1-F3) | Finding |
| V-D1 | The camera mount becomes a configured rotation `R_body_cam`, measured on the airframe, instead of the fixed matrix in [C3]. Guidance and tracking read it from config. The mount angle itself is open (V-F5) | Chosen |
| V-D2 | The touch-trial closing pass uses proportional navigation (PN) sent as acceleration setpoints. ArduCopter adds gravity to an acceleration setpoint, which gives the gravity feedforward. Changes [G5] and [F6] (a `type_mask` that uses acceleration). Pure pursuit [G3] stays for reaching the hold point and for standoff trials | Chosen |
| V-D3 | The approach may climb into a target above the vehicle, which suits the cone airframe. The touch-trial hold point no longer has to be level with the target. This supersedes E1-D3 for touch trials; E1-D3 stays Provisional for standoff trials. The implementing PR sets the hold-point geometry | Chosen |
| V-D4 | Touch hold distance about 2 m, a prime parameter. Consequence: with `f` = 806 px and `W` = 1 m the box is 403 px (0.21 of the frame) at 2 m, so the [G4] fill clause cannot hold at the §9 `alpha` of 0.4. `alpha` drops to about 0.18-0.2, or the commit gate changes, in the implementing PR | Chosen (number Provisional) |
| V-D5 | During the pass, guidance steers until the box touches the frame edge, the tracker loses the target, or time to contact falls below about 0.2 s (V-D9). Then it holds the last command and steers no more. For a 1 m balloon with the 100 deg lens this happens at about 0.4-0.6 m. This replaces the open-loop flight from the commit point in [G5]; brake, climb and `pass_done` stay | Chosen (numbers Provisional) |
| V-D6 | Setpoints run at 50 Hz during the pass (today 10 Hz, §9 `setpoint_hz`). Changes [G11] and §9 | Chosen (number Provisional) |
| V-D7 | FC parameter changes are allowed (for example `ANGLE_MAX`, acceleration setpoint support), in the next phase. The operator sets them; fc_link keeps no parameter-write path ([F1]) | Chosen |
| V-D8 | MPC guidance. Trigger: a recorded scenario or flight where PN fails | Deferred |
| V-D9 | Time to contact `tau = w / (dw/dt)` from the tracker's smoothed width and width rate. It needs neither `f` nor `W`. Uses: hand-off timing (V-D5), a cross-check of size range [G2] (a mismatch means the primed `W` is wrong and is logged), and a second closing-speed source for PN. It needs an additive width-rate field on the track packet [P2]; the implementing PR names it. Valid only while closing, and not once the box clips the frame edge | Chosen |
| V-D10 | Track in world angles. Each box center is de-rotated with the attitude at capture time ([G1]) before the tracker filters it, so the vehicle's own rotation does not break association and PN gets the line-of-sight rate directly. Changes [K1] and the tracker input. Wire changes stay additive | Chosen |
| V-D11 | Lens calibration includes distortion. Box centers are undistorted before use, and `camera_sim` gets the same distortion model. The ChArUco calibration itself happens later | Chosen |
| V-D12 | A detection dropout during the pass holds the last command for up to `coast_cap`. If the track dies, the pass ends: brake, climb, `MISS`. Changes [G10] for `COASTING` inside the pass | Chosen |
| V-D13 | Abort stays RTL for both the Wi-Fi abort and the radio switch. No change | Chosen |
| V-D14 | A target may be tethered (a string to the ground). Accepted risk: the string can wrap a prop, which loses the balloon or crashes the vehicle. Fly over soft ground with no people near, and keep the string off the approach side | Chosen |
| V-D15 | The SITL harness gets wind and gusts (SITL wind parameters) in new scenarios. Gate scenes S1-S6 and their seeds [S7] do not change | Chosen |
| V-D16 | `camera_sim` gets bad frames: false boxes, dropped frames, latency jitter, in new scenarios. Gate scenes do not change. Random dropout in a standoff hold still meets E1-F16 | Chosen |
| V-F2 | SITL flies model `+` (a plus frame). The vehicle is an X with a top-heavy tower. ArduPilot SITL takes a JSON frame file (`--model quad:<file>.json`: mass, diagonal, inertia, motor positions, drag), so the cone can be modeled. The numbers (all-up mass, tower height, battery position, motor diagonal) are open: from CAD or estimated, labeled Provisional | Finding |
| V-F3 | Sim coverage gaps: every touch run uses a static balloon; `ThrownPlane` exists but no scenario uses it; the only false box is S6's scripted bird; targets sit 2-18 deg above the horizon (the old level-approach geometry); the camera model has no distortion and the old mount. New scenarios cover these; gate scenes stay | Finding |
| V-F4 | No YOLO model exists yet, and the percepd v0 source is still missing (E1-F1, E1-F2). Detector-in-the-loop sim waits for a model. A 3D renderer can make labeled training images | Finding |
| V-F6 | Airframe numbers from the Onshape assembly `Skyweave-000-ASM` (2026-11-20). CAD mass 0.527 kg (1.162 lb); Samuel estimates the real vehicle at CAD + 0.25 lb or a little more, so about 0.64 kg. Inertia about the center of mass from CAD: Ixx 2.24e-3, Iyy 2.22e-3, Izz 1.99e-3 kg m^2 (about 2.71e-3, 2.69e-3, 2.41e-3 scaled to 0.64 kg, assuming the added mass spreads like the rest). X frame: motor-to-motor diagonal about 210 mm (side 149 mm). Overall about 256 x 257 mm at the prop tips, 221 mm tall. The battery stands upright in the tower above the props, the nose electronics and camera sit on top, and the FC/ESC stack and feet sit below the props. The center of mass is about 19 mm above the prop plane and about 11 mm sideways from the motor center in CAD (check this on the vehicle). Motors T-Motor Velox V2207 1750KV (the CAD uses a stand-in motor model); props Gemfan Hurricane 51433 (5.1 in diameter, 3.3 in pitch, tri-blade; an `R` on the hub marks the reverse-rotation props; the CAD model shows a 51466); battery 6S 1300 mAh. Thrust is not measured (a thrust stand comes later): T-Motor lists about 740 g at 60 % throttle with a 5.1 in prop, and full throttle is likely 1.3-1.6 kg per motor, so thrust-to-weight is about 8-10 and hover throttle about 0.2. ArduCopter's hover-throttle learning gives a measured hover throttle from the first hover flight. The mesh measurements are from the STL export (inches). These numbers feed the SITL frame file (V-F2) and the 3D model. None is Measured | Provisional |
| V-F5 | Open for Samuel: camera tilt (proposal: about 30 deg forward of the nose axis, wide axis along pitch; Samuel is weighing a yaw rotation of the wide axis between scan and pursuit); lens 100 deg vs 80-90 deg; the 3D sim design (proposal: render-only, with SITL keeping the physics); E1-F5, E1-F15, E1-F16, E1-F17 and E1-F23 stay open | Finding |

### 11.2 E2 planning decisions (2026-11-21)

Samuel worked through the blockers to phase E2 with Claude on 2026-11-21 and
made the decisions below. `PHASE_E2_BRIEF.md` builds on them. As in §11.1,
they authorize clause changes; the clauses above stay as written until the PR
that implements a decision edits them and cites its ID. Numbers are
Provisional; none is Measured. [F1] still holds.

| ID | Item | Label |
| --- | --- | --- |
| E2-D1 | Gate policy for E2. E2 gets its own gate, frozen before any E2b tuning: the cone frame ([S12] `cone_x`), the E2-D2 mount and lens, touch trials only, seeds `sha256("E2-gate:<scene>:<i>")`. Its pass numbers are declared before its first run. The E1 gate (S1-S6, [S7] seeds) does not change and stays green on every E2 PR as a regression check. The configured mount (V-D1) lets it keep the [C3] forward camera | Chosen |
| E2-D2 | Camera mount and lens for now (part of V-F5). The optical axis lies along the nose, which is the thrust axis, so the camera looks straight up at hover. The wide image axis (u) lies along body x: `R_body_cam = [[1,0,0],[0,-1,0],[0,0,-1]]` (mount name `cone_up`). Lens 100 deg HFOV, `f` about 806 px on 1920 px (V-F1), half-angles 50.0 deg along u and 36.7 deg along v. The 30 deg tilt proposal and the yaw rotation between scan and pursuit are set aside, not rejected. Samuel confirmed the wide axis along body x (fore-aft) on 2026-11-21 | Chosen (numbers Provisional) |
| E2-D3 | E2 runs in two parts. E2a: camera and tracker geometry (V-D1, V-D9, V-D10, V-D11, E2-D5). E2b: the PN pass (V-D2 to V-D6, V-D12) and the E2 gate. PN needs E2a's world line-of-sight rate | Chosen |
| E2-D4 | The `v_max` safety floor ([S8], E1-F15) is ignored in E2 for now. The E2 gate does not check it; its scorecards report peak commanded and peak true speed. The E1 gate keeps the floor. E1-F15 stays open. The [F6] hard limits still apply to every sent setpoint | Chosen |
| E2-D5 | The tracker runs inside the companion process, so de-rotation (V-D10) has the companion's attitude history at hand. Detection packets go to the companion. The companion publishes track packets on the track port for external listeners, as it publishes mission_state. No new packet type. Changes [C10] | Chosen |
| E2-D6 | Search from below. With E2-D2 a level vehicle sees only targets at least 40 deg above its horizon (the u half-angle); the yaw scan sweeps that edge around. With a height gap `dh` below the target, the target is visible within about `1.19 * dh` horizontally. The operator flies the search below the balloon. E2 probes and the E2 gate put targets 40-80 deg above the vehicle at search | Chosen |
| E2-D7 | Standoff trials are out of E2. A standoff hold on a balloon is not practical for now; E2 focuses on intercept (touch trials). Kites and thrown planes are out of E2 too. Standoff code stays, and S1, S3 and S6 keep running in the E1 gate. E1-D3 (standoff geometry) and E1-F16 (the hold under dropout) stay open | Chosen |
| E2-F1 | ArduCopter 4.7.0 acceleration setpoints, from a read of the source at tag Copter-4.7.0 (not yet run). `SET_POSITION_TARGET_LOCAL_NED` masks: acceleration plus yaw rate is 1087; velocity plus acceleration plus yaw rate is 1031. The force bit (512) must stay clear: with acceleration used, the handler then holds position instead. The acceleration is kinematic: ArduCopter adds gravity and turns it into lean and thrust, as V-D2 assumes. `GUID_TIMEOUT` defaults to 3 s; on timeout the velocity and acceleration targets go to zero. The position controller jerk-limits the acceleration input (`PSC_JERK_NE`), which adds lag. Acceleration-only mode skips the controller's speed limiting; velocity plus acceleration keeps it. The handler applies each message at once, with no rate limit. E2b proves each point on SITL before PN relies on it | Finding |

### 11.3 E1 leftover decisions (2026-11-24)

Samuel closed five open E1 findings with Claude on 2026-11-24, taking the
recommendation for each. The PR that records these decisions also edits the
clauses and the code. Gate scenes S1-S6 and their seeds do not change.
Numbers are Provisional; none is Measured. [F1] still holds.

| ID | Item | Label |
| --- | --- | --- |
| E1-D12 | Closes E1-F5. `approve_engage` may carry `params.track_id`. The mission accepts it only when that id is the current candidate; a different id is `rejected_state`. The UI page always sends the id of the candidate it displays, and sends no approve when it displays none. The radio approve carries no id and works as before. Additive in wire v1. Changes [P5], [M5], [M8]; adds [U7] | Chosen |
| E1-D13 | Closes E1-F15. The trial `v_max` caps every velocity command in every direction: the altitude hold and the terminal climb obey it too. Adds [G12]; changes [G5], [G10]. The E1 gate primes 2.5 m/s, above `v_climb` and `v_alt_max`, so its commands do not change. E2-D4 still governs the E2 gate | Chosen |
| E1-D14 | Closes E1-F16. The standoff hold clock keeps running through a short dropout (the track coasting, up to `coast_cap`). It restarts only when the vehicle leaves the tolerance, attitude is degraded, or the track is lost. `hold_complete` is still emitted only on a real (non-coasting) packet. Changes [G7]. Standoff trials stay out of E2 (E2-D7); this makes them testable with bad frames (V-D16) | Chosen |
| E1-D15 | Closes E1-F17. Only a state poll that carries the right token is a ground heartbeat. Another device on the access point can still read the status page, but it no longer holds off the T18 ground-link failsafe. A page reload must not break the ground link (Samuel, 2026-11-27): the page keeps the token in the tab's `sessionStorage`, so a reload of the same tab keeps the heartbeat. Accepted consequence: a closed tab, a new tab or a browser restart stops the heartbeat until the operator types the token again, and T18 then sends the vehicle to `RETURN` after `ground_link_timeout_ms`. The token now also travels in clear on each poll (E1-F14 already accepts clear text for controlled demos). Changes [U3], [U6] | Chosen |
| E1-D16 | Closes E1-F23. The switch position that brief 3.11 calls MANUAL is `ALT_HOLD` on the vehicle: the FC holds height while the pilot steers, which is the easiest takeover by sight. `STABILIZE` is the fallback if `ALT_HOLD` behaves badly on the first manual flights. The operator sets the flight-mode parameters (V-D7). The SITL harness keeps `STABILIZE` for that position, because the mission asks only whether the mode is `GUIDED`. No code change | Chosen (Provisional until the first manual flights) |
