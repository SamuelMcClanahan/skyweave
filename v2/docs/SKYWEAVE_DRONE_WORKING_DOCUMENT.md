# SkyWeave drone system: working document

**Revision:** 2026-07-28  
**Status:** human-directed working design; measurements still outrank estimates  
**Role:** core drone notes; section 15 is the single open-question register  
**Primary source:** `skyweave_interceptor_notes(1).md` rev 2, supplied by Samuel

## 1. How to read this document

This is the single working summary for the ground system, turret, drone,
simulation plan, onboard perception, vehicle CAD, and implementation order.

It is also the core set of drone notes. Section 15 holds every open drone
question in one list; a question that is not in section 15 is not tracked. Add
new questions there rather than in a side document, and retire one only when a
recorded result answers it.

The earlier drone documents now live in [`background/`](./background/) as
detailed source material:

- [`background/DRONE_RESEARCH_VEHICLE_REQUIREMENTS.md`](./background/DRONE_RESEARCH_VEHICLE_REQUIREMENTS.md)
  — the original integration brief. Still the fullest treatment of the Cubie
  A7Z constraints, separated power domains, camera requirements, and vendor
  references.
- [`background/DRONE_CAD_FREEZE_SHEET.md`](./background/DRONE_CAD_FREEZE_SHEET.md)
  — the detailed CAD parameter tables, material schedule, waterjet and carbon
  rules, hardware interface reservations, and frame-alternative comparison.

Neither overrides this document. Where they disagree with it, this document
wins; where they carry detail this document does not repeat, use them. Both
still describe a 4S first battery and a 2:1/2.5:1 thrust target, which sections
11.1 and 11.3 have replaced with the 6S candidate and the 4:1 goal.

Source priority is:

1. Samuel's rev 2 notes and current conversation decisions;
2. measured results from the real hardware and simulator;
3. the earlier SkyWeave research, implementation, and drone CAD documents;
4. outside papers and vendor claims.

The earlier documents fill gaps, but they do not silently override Samuel's
notes. Where they disagree, the conflict is recorded in section 14.

Labels used below:

- **Chosen:** current design direction.
- **Provisional:** useful starting point, not yet measured.
- **Measured:** backed by a recorded test on the actual configuration.
- **Deferred:** intentionally later.
- **Rejected:** not part of the working design.

## 2. What the system is

SkyWeave is a distributed multi-camera system that detects and tracks flying
objects. Fixed camera nodes provide wide coverage. A Jetson fuses their
observations into a coarse 3D track. A later narrow-FOV turret can hold a
high-resolution view. A small drone can then receive a cue, collect closer
tracking data, and test ground-to-air handoff and onboard visual lock.

The basic split from Samuel's notes is retained:

```text
ground layer:
  detection, classification, 3D tracking, cueing, recording,
  human authorize/abort, and initial safe setpoints

onboard layer:
  limited visual acquisition after a cue, relative tracking,
  logging, and later high-level pursuit/standoff control

flight controller:
  attitude stabilization, motor output, arming, and failsafe
```

The drone never begins with open-world search. It starts with a known track and
a bounded search window from the ground system.

## 3. Safety boundary

Samuel's source notes include deliberate physical-contact geometry and an
impact load path. Those details are not carried into this working design.

This document does not specify collision guidance, an impact aimpoint, a rigid
contact cone, an axial impact spine, a brittle contact tip, or structural
optimization for ramming. The working vehicle is a non-contact tracking and
data-collection platform with a rounded, removable sensor fairing or soft
bumper. Any soft-capture experiment would need a separate safety review.

Live tests use owned targets, a controlled site, conservative separation,
human authorization, a continuous abort path, and an FC-owned failsafe.

## 4. End-to-end architecture

### 4.1 Fixed camera layer

**Chosen:** RV1106/Luckfox Pico class nodes with fixed SC3336 cameras provide
wide-area motion observations.

The edge node owns:

- camera capture and timestamp metadata;
- luma/background modeling and motion extraction;
- compact observation/tracklet packets; and
- node health and calibration revision identifiers.

The edge node does not own multi-camera association, final 3D geometry, or the
global track identity.

### 4.2 Jetson orchestrator

**Chosen:** the Jetson Orin Nano owns:

- packet validation and clock mapping;
- multi-camera correspondence;
- sparse/local voxel evidence for candidate search where useful;
- continuous multi-ray refinement for the final 3D measurement;
- outlier rejection, covariance, and the EKF track lifecycle;
- turret cueing, drone cueing, recording, and visualization; and
- human authorize/abort state.

Voxel and continuous-refinement results are not independent measurements. A
voxel candidate may seed the continuous solver; the refined measurement is what
enters the track filter.

### 4.3 Turret

**Deferred until the fixed-camera stack passes its synthetic and wired gates.**

The turret is a two-axis gimbal with a narrow-FOV global-shutter camera. It is
slaved to the mesh track and does not independently search the full sky. Its
jobs are higher-resolution tracking, cue refinement, and experiment recording.

Turret measurements may improve bearing quality and association. They do not
magically add a useful triangulation baseline if the turret is effectively
co-located with another camera.

### 4.4 Drone

The first drone loop can have no onboard perception:

```text
SkyWeave tracks target and drone
  -> Jetson computes safe pursuit/standoff setpoints
  -> uplink sends setpoints and track metadata
  -> FC flies the vehicle
  -> human can abort continuously
```

This is the first complete ground-to-air integration test. It exercises the
track, communications, timing, command path, FC interface, and logging before
adding onboard vision.

When onboard vision is added, the ground system keeps sending the target track
until the drone explicitly confirms visual lock.

## 5. Handoff, time, and communications

### 5.1 Handoff message

The handoff should contain at least:

```text
track ID and state revision
target position and velocity
target covariance and systematic-bound metadata
state timestamp, clock domain, and measured age
validity/expiry time
camera/turret cue or expected image region
authorization state and abort state
```

Samuel's note proposes at least 10 Hz and less than one second of latency.
**Conflict:** 10 Hz is a reasonable initial rate floor, but one second is much
too stale for close-loop motion. At 5 m/s, one second is 5 m of unmodeled
travel; at 20 m/s it is 20 m. Freshness must instead come from an error budget:

```text
maximum state age <= allowed position error / relative speed
```

The receiver should propagate the state from its timestamp to the current time
using the declared motion model and uncertainty.

### 5.2 Time synchronization

**Chosen:** start with PTP on the wired ground network and measure the result.

The arithmetic in the source notes is correct:

- `5 m/s × 20 ms = 0.10 m` of time-correlated position error;
- `20 m/s × 10 ms = 0.20 m`.

However, clock offset is only one term. Exposure timing, rolling-shutter row
time, userspace capture delay, network age, inference time, and FC/motor
response are separate terms. PTP is not accepted merely because it is enabled.

Every frame and command log should retain:

- exposure or best-known sensor timestamp;
- timestamp source and clock domain;
- receive time and state age;
- capture, inference, transport, guidance, and FC-command latency; and
- uncertainty or quality flags.

Use GPS-PPS or tighter hardware timing only when measured PTP/capture behavior
cannot meet the error budget.

### 5.3 Uplink and failsafe

**Provisional:** an ELRS-class low-latency link carries cues and high-level
commands. It does not replace an FC-owned safety path.

Link loss must cause a configured safe behavior such as hover, return, land, or
disarm depending on the test environment. The exact behavior is tested with
the Cubie disconnected and with the cue link deliberately removed.

## 6. Onboard stack

### 6.1 Camera

**Chosen requirements:**

- global shutter;
- at least 60 fps as the initial target;
- fixed lens and manual exposure/gain;
- rigid, measurable optical datum;
- timestamp access or a documented host capture timestamp;
- mounted forward/upward for the intended tracking geometry; and
- replaceable flat lens window, calibrated in place if used.

An OV9281-class monochrome module is a candidate, not a frozen part. Radxa's
official camera list does not establish OV9281 support, so the CAD preserves a
USB/UVC global-shutter fallback.

### 6.2 Companion computer

**Chosen:** Radxa Cubie A7Z for camera processing, relative tracking, logging,
and later high-level commands.

Known vendor envelope:

- nominal board outline `65 × 30 mm`;
- one four-lane MIPI CSI interface;
- 5 V input and a 5 V fan header; and
- vendor-recommended operating range of 0–50 °C.

The source-note estimate of about 100 g installed is **provisional**. Weigh the
actual board, storage, heatsink/fan, camera adapter, cables, antenna, regulator,
and tray.

Use a dedicated filtered/current-limited 5 V regulator, initially reserved at
about 25–30 W until the actual workload is measured. Do not power the Cubie
from a small ESC BEC.

### 6.3 Flight controller

Use a reproducible, supported F7 or H7 FC with ArduPilot or PX4 if MAVLink
high-level commands are required. F4 remains possible in principle but is not
the preferred baseline because support, memory, UART count, and logging vary.

The FC always owns stabilization, motor output, arming, and failsafe. The Cubie
does not directly generate motor PWM.

## 7. Perception ladder

Add the next rung only when recorded evidence shows the previous one failing.

1. **No onboard perception.** Ground-only cueing and safe setpoints.
2. **Classical CV.** Frame differencing, contrast/blob extraction, and a
   CV-KF/EKF within the cue window.
3. **Nano detector.** A small single-class model for clutter, weak contrast, or
   scale regimes where classical CV measurably fails.
4. **IMM or more advanced motion model.** Only if actual target maneuvers break
   the CV/CA tracker.

“Near-zero latency” and “60 Hz dumb beats 15 Hz clever” are useful instincts,
not measured facts. The binding metric is the complete exposure-to-control
loop together with tracking quality and false-lock behavior.

## 8. Data flywheel and evaluation

The ground track, onboard camera calibration, drone pose, and time mapping can
project the target into the onboard image. That projection can provide
automatic labels and an independent tracker score.

It is not automatically perfect ground truth. Label uncertainty includes:

- SkyWeave position/range uncertainty;
- ground and onboard clock error;
- drone pose and camera-extrinsic error;
- camera calibration and lens-window error; and
- ambiguity in which physical point on an extended target the track represents.

Development stages are:

1. **Shadow:** human/manual or scripted safe flight; log would-be commands.
2. **Assisted:** autonomy maintains a safe pursuit/standoff path while the
   human holds abort.
3. **Autonomous non-contact tracking:** only after shadow and assisted gates
   pass.

Score the system with:

- 2D cue/lock error in pixels;
- 3D tracking error where bounded truth exists;
- track acquisition and retention time;
- false-lock and drop rate;
- state age and full-loop latency;
- commanded-versus-flown trajectory error;
- safe closest-approach or standoff error; and
- compute, current, temperature, and battery sag.

Model mAP is secondary to the closed-loop system metrics.

The first physical target should be owned, slow, high contrast, soft, and used
in a controlled environment. If the paper-airplane/foam-glider class is below
SkyWeave's measured detection limit, increase the target size or contrast
before increasing model complexity.

## 9. Simulation-first plan

Simulation grows with the build, but it still uses the real contracts and
network path.

### 9.1 Ground stack first

The current fixed-camera milestone remains:

1. freeze coordinate, pixel, time, observation, covariance, and systematic
   bound semantics;
2. prove projection and triangulation analytically and with Monte Carlo;
3. freeze the canonical 800 ft EXP-001 scene and acceptance gates;
4. generate deterministic Blender frames and truth sidecars;
5. build the host foreground detector and `Observation2D`;
6. build direct/voxel hypotheses, continuous refinement, robust gates, and EKF
   track lifecycle;
7. inject image, geometry, detection, timing, and packet faults;
8. implement recorded-Y, protobuf, UDP, and replay paths;
9. replay the same data through the real RV1106 nodes, switch, and Jetson;
10. pass the wired synthetic acceptance report;
11. replace replay with real CSI/ISP and characterize PTS/GMM2/CCL; and
12. measure real sky, calibration, and pose stability before adding the turret
    or drone dependency.

Use Blender first. Add Isaac Sim only for a capability Blender lacks.

### 9.2 Drone simulation

After the ground track contract exists, add a drone dynamics/FC simulation
using the same messages as the real vehicle:

- ground-only safe setpoint loop;
- transport delay, loss, reorder, and link-loss failsafe;
- camera projection and onboard cue window;
- classical-CV tracker replay;
- camera/pose/time uncertainty;
- wind and actuator delay; and
- shadow-mode command logs.

The simulator must record evaluator-only truth and use the real packet schema.
It is not complete merely because a perfect simulated drone follows a perfect
target state.

## 10. Vehicle and CAD requirements

### 10.1 Chosen frame direction

- true-X quadcopter;
- 5-inch propeller class;
- approximately 220 mm opposite-motor-center diagonal;
- modular sandwich frame with replaceable arms;
- camera and Cubie near the vehicle centerline/CG;
- adjustable battery position; and
- 7-inch branch only if the measured 5-inch payload/thrust margin fails.

The former parameterized 4-inch alternative is no longer the active design.
It is too constrained for the present compute, camera, cooling, and power
budget.

### 10.2 Structural materials

The primary flight load path is carbon, not printed plastic:

```text
replaceable carbon arms
3 mm carbon lower/center plate as the starting point
2–3 mm G10 or carbon upper electronics deck
M3 through-fasteners, broad washers, and lock/captive nuts
rigid carbon/G10/aluminum camera datum
PETG-CF/G10 Cubie tray with insulation and airflow
TPU removable hood, feet, and soft bumpers
```

Because the source-note mass approaches 1 kg, 3 mm arms may not be sufficient.
CAD should support 3 mm and 5 mm replaceable arm/root configurations. Test the
same laminate, span, fastener spacing, load, deflection, and vibration before
choosing. Do not make the entire frame 5 mm by default.

Carbon is electrically conductive and can attenuate RF. Seal its edges, use
G10/Kapton/nylon barriers beneath electronics, keep cables away from raw edges,
and place receiver/GNSS antennas outside the carbon shadow where practical.

### 10.3 Printed parts and A1 Mini

The stock A1 Mini build envelope is about `180 × 180 × 180 mm`. Keep individual
printed pieces under roughly 170 mm and verify the slicer profile.

- PETG/PETG-CF: trays, brackets, ducts, and non-primary structure;
- 72D TPU or TPU-GF: thin removable sensor hood if its exact hardness/process
  is confirmed;
- softer 85A–95A TPU or silicone: feet, grommets, and soft bumper;
- PC: small heat-resistant parts after a print/process test;
- PA-CF/PPA-CF: later printer/material capability, not a first-build dependency.

The fairing is non-load-bearing, vented, and removable. Use 1.5–2 mm walls with
ribs and thicker local mounting bosses as a starting print. Split it into
bolted/keyed panels if necessary. Do not place thin printed material close to
the propeller tips.

The camera stays on a rigid datum independent of the flexible hood.

### 10.4 CAD master parameters

```text
prop_diameter, pitch, blade_count
motor_diagonal, arm_width, arm_thickness, root_doubler
motor_pattern, motor_screw_diameter, prop_interface
lower_plate_thickness, upper_deck_thickness, stack_height
fc_pattern, esc_pattern, cubie_keepout, camera_datum_offset
battery_envelope, payload_mass, strap_spacing, CG target
prop_to_shell_clearance, antenna_keepout, landing_clearance
fairing_wall, fairing_split, ventilation_keepout
```

Target at least 5 mm static clearance from every prop disc, with 8–10 mm as the
initial shell/wire/antenna target until arm and shell flex are measured.

## 11. Propulsion and mass budget

### 11.1 Current candidate

Samuel's notes now make 6S the preferred candidate:

```text
propeller: 5-inch, approximately 4.7–5.1 pitch, three blade
motor:     2207/2208 class, approximately 1700–1900 KV
battery:   6S, short-duration pack selected after current/sag testing
ESC:       40–60 A 4-in-1, 6S capable, with current telemetry
goal:      compact, high control authority, roughly 3–4 minute test mission
```

These are **provisional candidates**, not settled parts. Test at least a lower-
pitch and the proposed higher-pitch prop. Record static thrust, current,
voltage sag, motor/ESC temperature, vibration, and soundness after repeated
bursts. A higher-pitch tri-blade can improve compact thrust/control response but
costs current and heat.

At `1900 KV × 25.2 V`, unloaded speed is about `47,880 rpm`. For a 14-pole
motor, that is roughly `335,000 electrical rpm`; check the actual motor pole
count and ESC firmware ceiling.

Motor resistance matters through copper loss (`I²R`), but the source-note
claim that `0.025 Ω` versus `0.07 Ω` means about 20 °C cooler is not accepted
without the same prop/current/airflow and a thermal test.

### 11.2 Weight arithmetic

The source-note component estimates are retained:

| Item | Source estimate |
| --- | ---: |
| Frame | 130 g |
| Four motors | 152 g |
| ESC | 45 g |
| FC and wiring | 60 g |
| Fairing/support allowance | 80 g |
| Camera | 40 g |
| Cubie installed allowance | 100 g |
| 6S short-duration battery | about 200 g |
| 6S larger battery | about 385 g |

The listed fixed items total 607 g. With the two battery estimates, the direct
sum is about 807–992 g, not 1000–1150 g. Receiver, buck, antennas, props,
fasteners, straps, landing parts, and growth margin can raise it into that
range. Build a live measured mass table instead of preserving either total as
fact.

### 11.3 Thrust requirement

```text
measured TWR = total measured static thrust / measured all-up weight
```

Samuel's desired design goal is at least 4:1. That is an ambitious goal for a
near-1 kg 5-inch build and must be proven with the exact motor, prop, pack, and
ESC. The earlier 2.5:1 value remains a lower control-authority reference, not
the new design goal.

The source claim that 5-inch specific thrust is fixed at about 4.5 g/W by disk
loading is too strong. Disk area constrains induced power, but propeller,
motor, ESC, voltage, operating point, and airflow efficiency still matter.

The claim that a 2500 mAh pack gives about 12 minutes is also unverified. The
current mission is about 3–4 minutes, so begin with the short-duration 6S pack
envelope and measure hover/burst current, usable capacity, sag, and landing
reserve.

### 11.4 Static propulsion model: OpenCalc

**Chosen:** the screening tool for §11.1 candidates, and the place where
§11.3's arithmetic gets written down instead of argued.

OpenCalc is a Python-first static multicopter propulsion calculator built for
this project. It is a transparent, scriptable stand-in for the static gauges in
eCalc's `xcopterCalc`: a complete battery/ESC/motor/prop/airframe stack goes in,
and the intermediate operating point comes out where it can be inspected rather
than trusted.

It lives in this repository at [`tools/opencalc/`](../../tools/opencalc/), with
its own [README](../../tools/opencalc/README.md) covering the model equations,
data provenance, and validation fixtures. The vendored copy is the project
reference; the separate development checkout outside this repository is not, and
the two can drift. Re-vendor rather than edit both.

**What it runs**

The runtime needs only the Python standard library, so no virtual environment is
required to get a number:

```text
cd tools/opencalc
python3 -m opencalc analyze validation/cases/ecalc_case_3.json
python3 -m opencalc sweep validation/cases/ecalc_case_3.json \
  --var weight --range 800:1400:50
python3 -m opencalc gui        # local browser dashboard on 127.0.0.1:8765
```

`numpy`, `scipy`, `matplotlib`, and `PyYAML` are optional conveniences for
sweep plotting and YAML input. A stack is a JSON or YAML file with one shared
schema, so a candidate can be version-controlled next to the part list rather
than retyped into a web form.

**What it answers**

At a given stack it solves the RPM where motor torque equals propeller torque,
then reports thrust, TWR, per-motor and pack current, sagged pack voltage,
hover throttle and hover endurance, specific thrust, eRPM, disk loading, figure
of merit, ESC continuous/burst margin, battery C-rating load, and a
steady-state motor temperature estimate. It emits warnings rather than silent
clipping when eRPM, ESC current, C-rating, or figure of merit leave their
plausible ranges.

That maps directly onto questions this document is currently holding open:

- §11.1: whether a 2207/2208, 1700–1900 KV, 6S, high-pitch 5-inch stack lands
  inside the ESC and eRPM ceilings before anything is bought;
- §11.2/§11.3: what TWR and hover current the current mass table implies, and
  how both move as mass grows — the weight sweep is the direct form of this;
- §11.3: whether the rejected "about 4.5 g/W fixed by disk loading" claim
  survives contact with a real prop/motor/voltage operating point;
- §15.8: what pack capacity a 3–4 minute mission with landing reserve actually
  needs.

**Label discipline**

Every OpenCalc output is **Modeled**. Nothing it prints is **Measured**, and no
number from it may be promoted to **Measured** in this document without a
recorded bench test on the exact configuration.

The tool is built to make that boundary visible rather than convenient. It
prefers a measured propeller table; an interpolated propeller, an estimated
thermal resistance, or a user override is marked as such in the report's model
notes, and a missing motor winding resistance is surfaced as a warning because
current and temperature are sensitive to it. Data provenance is ranked in its
README — UIUC/Brandt-Selig wind-tunnel tables first, then APC published data,
then independent bench measurements, then specification sheets. Read the model
notes on any report before quoting its numbers.

**Scope limits**

OpenCalc v1 evaluates the static point (`J = 0`). It does not model forward
flight, dynamic maneuvers, descent behavior or VRS, voltage transients or
transient battery chemistry, prop-wash and frame interference, ESC commutation
detail, or burst thermal transients.

So it cannot settle the §14 high-pitch tri-blade descent/VRS concern, and its
steady-state motor temperature is not a substitute for the repeated-burst
thermal test in §13 phase 2. A static model that says a stack is marginal is
useful; a static model that says a stack is fine is a permission to test, not a
result.

**Closing the loop with bench data**

Once the thrust stand runs, feed the measurements back into the same tool rather
than into a separate spreadsheet. Setting `mode: measured` with a CSV path
bypasses the motor/prop solver for an exact tested combination and interpolates
the recorded curve, while the battery, ESC, eRPM, and endurance checks still
run. CSV columns are `throttle_pct`, `thrust_g`, `current_a`,
`loaded_voltage_v`, `rpm`, and `power_w`.

The validation fixtures accept modeled-versus-reference agreement inside a
±15 percent band (±10 percent on the manufacturer full-throttle points). When
our own bench data misses that band, record the disagreement and find the wrong
term — RPM, current, sag, or coefficient — before adjusting a coefficient to
make the miss disappear.

## 12. Hardware checklist

Before final carbon or fairing CAD, select and measure:

```text
4 motors with drawing, pole count, mounting pattern, resistance, and thrust data
5-inch prop candidates with exact diameter/pitch/blade count
6S-capable 4-in-1 ESC with current sensing and verified firmware/eRPM limit
supported F7/H7 flight controller and soft-mount hardware
receiver, transmitter, buzzer, and independent kill/abort path
6S LiPo candidates, connector, capacitor, charger, and safe storage
5 V high-current buck with filtering/current limiting
Cubie A7Z, storage, heatsink/fan, antenna, and tray
global-shutter camera, lens, cable/adapter, and USB fallback
independent pilot-view camera/link if required for manual flight
carbon laminate, G10 deck, fasteners, insulation, straps, and spares
```

Screen each motor/prop/pack/ESC combination through `tools/opencalc` (§11.4)
before ordering it, and keep the stack file with the part list. Manufacturer
drawings supply the model inputs it needs: pole count, KV, winding resistance,
no-load current, mass, and any published thrust curve. Capture thrust-stand
results in the tool's measured-CSV columns from the first test so the modeled
and measured curves stay in one place.

## 13. Implementation sequence

| Phase | Do | Figure out | Done when |
| --- | --- | --- | --- |
| 0. Freeze scope/contracts | Freeze frames, timestamps, observation schema, covariance, handoff, expiry, and abort semantics | Exact state freshness and safety-separation budgets | Replays and message tests reject stale/invalid data deterministically |
| 1. Ground synthetic spine | Complete EXP-001 through local and physical-node replay | Whether geometry, detector, timing, and packet gates pass | Same saved Blender data passes through real nodes/switch/Jetson with a recorded report |
| 2. Vehicle bench/CAD | Model candidate stacks in `tools/opencalc` (§11.4), then weigh parts, make fit mule, test carbon coupons, thrust stand, 6S power, Cubie thermal, and camera capture | 3 vs 5 mm arms, real AUW/TWR, pack size, ESC/eRPM, fairing envelope, and where the static model disagrees with the stand | Measured mass/CG/thrust/current/sag/thermal/vibration budget supports the prototype, and each model-versus-measurement gap is recorded rather than tuned away |
| 3. Manual vehicle | Build prototype, tune FC, log blackbox, prove receiver/abort/link-loss behavior | Whether the payload or fairing causes control/vibration problems | Stable contained/manual flight with Cubie powered and logging |
| 4. Drone simulation | Add drone dynamics and real handoff packets to Blender/SITL/HITL replay | Required cue rate, latency, prediction, and safe standoff controller | Faulted sim passes expiry, dropout, abort, and trajectory-error gates |
| 5. Ground-only shadow | Ground tracks target/drone and logs would-be setpoints while human flies | Whether ground state is accurate/fresh enough | No unsafe/stale command; complete synchronized dataset and command trace |
| 6. Assisted non-contact | FC accepts bounded high-level setpoints with human abort | Tracking retention, state age, and flown trajectory error | Controlled pursuit/standoff test passes predefined separation and abort gates |
| 7. Onboard classical CV | Add cue-window blob/contrast tracker and KF/EKF | Whether clutter, scale, exposure, or latency breaks it | Measured lock/retention/latency gates pass on held-out sim and real recordings |
| 8. Model escalation | Add nano detector, then IMM only after recorded failure | Which failure each addition fixes | Each added component improves a named metric without breaking latency/thermal limits |
| 9. Turret and richer field tests | Add slaved narrow-FOV turret and measured outdoor calibration | Whether it materially improves track/cue quality | Improvement is demonstrated against the fixed-camera baseline |

## 14. Conflict and truth ledger

| Topic | Samuel's rev 2 notes | Earlier working docs / review | Working resolution |
| --- | --- | --- | --- |
| Physical contact | Rigid cone, spine, impact geometry described as settled | Earlier drone brief explicitly excluded impact design | **Rejected from this document.** Rounded sensor fairing and non-contact tracking only |
| Frame size | 5-inch / 220 mm settled; 7-inch only if payload forces it | 5-inch primary with 4-inch alternate | **Chosen:** 5-inch / 220 mm; 4-inch dropped; 7-inch contingency |
| Battery voltage | 6S settled | Earlier brief began with 4S for simplicity | **Provisional 6S candidate** because it matches current high-thrust goal; validate the complete system |
| Mission duration | 2500 mAh / about 12 min also discussed | Current conversation prefers about 3–4 min | **Chosen goal:** 3–4 minute test mission; pack selected from measurement |
| TWR | 4:1 floor, 4.0–4.8 estimate | Earlier safe research target 2.5:1 | **Goal:** 4:1; **unproven** at current mass; record 2.5:1 as lower reference |
| Frame thickness | Stiff CF with reinforced junctions | 3 mm baseline; local 5 mm only after test | **CAD both:** 3 mm center and replaceable 3/5 mm arm/root options; coupon/vibration decides |
| Printed structure | PETG/PETG-CF called structure | Printed frame rejected as primary load path | **Resolved:** carbon carries flight loads; prints carry trays, hood, ducts, and local brackets |
| FC | F4/F7 | Supported F7/H7 preferred | **Chosen:** supported F7/H7 unless an exact F4 proves every required interface |
| Handoff latency | at least 10 Hz, less than 1 s | Prior work treats timing as an error-budget input | **Conflict:** 10 Hz is a floor; 1 s is too stale. Derive maximum age from speed/error |
| PTP | adequate now | Must characterize real PTS/clock path | **Start with PTP, but measure it.** Sync does not replace latency measurement |
| Classical CV | full rate, near-zero latency | All latency must be profiled | **Working principle only;** accept after exposure-to-command measurement |
| Auto-labels | free bounding boxes from projection | Calibration/time/track errors remain | **Useful weak/strong labels with uncertainty,** not automatic perfect truth |
| High-pitch tri-blades | chosen for terminal grip; VRS warnings ignored | Initial brief favored moderate pitch and conservative tests | **Provisional prop candidate;** never ignore descent/VRS handling or current/thermal tests |
| Low motor resistance | fixed temperature benefit claimed | Temperature depends on full operating point | Prefer documented low loss, but reject fixed 20 °C claim without test |
| Specific thrust | about 4.5 g/W fixed by disk loading | System efficiency depends on more than disk area | **Rejected as a fixed constant** |
| Battery endurance | 2500 mAh gives about 12 min | No measured hover-current budget | **Unverified;** determine from usable energy and measured current |

## 15. Open questions

This is the complete drone question register. Questions 16-24 were consolidated
from the two documents now in `background/`. Their duplicates of questions
already listed here, and the ones this document has since decided — 4-inch
versus 5-inch, 4S versus 6S, the motor/prop/pack selection — were dropped rather
than carried forward.

1. Can the complete synthetic/wired SkyWeave stack meet its geometry and
   timing gates before a drone is added?
2. Can SkyWeave track a paper-airplane/foam-glider-class target, or should the
   controlled target be larger and higher contrast?
3. What state rate and maximum age follow from the actual speed and allowed
   position-error budget?
4. What end-to-end exposure-to-command latency does the A7Z classical-CV path
   achieve?
5. Which exact global-shutter module works on the A7Z, and is USB required?
6. What is the real installed Cubie/camera/cooling mass and peak 5 V power?
7. Does the proposed 2207/2208, 1700–1900 KV, 6S, high-pitch 5-inch system meet
   thrust, eRPM, current, sag, vibration, and temperature limits?
8. What pack capacity actually gives a safe 3–4 minute mission with landing
   reserve?
9. Do 3 mm arms pass on the near-1 kg vehicle, or are 5 mm replaceable arms or
   local root doublers required?
10. What fairing footprint fits the A1 Mini, preserves cooling, and clears the
    measured prop/arm flex?
11. What PTP/PTS/camera timestamp quality is achieved on the actual ground
    network and edge nodes?
12. What fixed-camera survey/calibration method provides the required real-world
    pose accuracy? Time synchronization does not solve camera pose.
    Evidence (Modeled, D1 error budget 2026-08-05, `v2/docs/D1_ERROR_BUDGET.md`):
    at 800 ft with a 20-25 m baseline, 0.1 degrees of camera rotation error
    alone costs about 6.5 m p95 range error, roughly three times the
    centroid-noise term. Camera rotation accuracy is the dominant error
    source; the survey/calibration method must budget for well under 0.1
    degrees or the 5 m goal is unreachable regardless of detector quality.
13. Which turret hardware and slaving controller are justified after the base
    system passes?
14. What legal/site/radio/airspace constraints apply to each controlled test?
15. Does the `tools/opencalc` static model (§11.4) reproduce the thrust stand
    within its ±15 percent band on the exact chosen motor, prop, pack, and ESC?
    If not, which term — RPM, current, sag, prop coefficients, or thermal
    resistance — is wrong, and does the answer change the candidate stack?
16. At the measured all-up mass, what companion-payload mass is actually
    allowable, and how much growth margin is left after it?
17. Which exact Cubie A7Z RAM/storage variant, heatsink, and fan arrangement is
    the build standard, so that its mass and thermal behavior mean one thing?
18. Which FC firmware target — Betaflight for manual flight, or ArduPilot/PX4
    for the later high-level command path — and does that choice constrain the
    F7/H7 board selection in §6.3?
19. Which 6S battery connector, charger, storage, and transport procedure is
    standard, and does the connector survive the measured burst current?
20. Where is the onboard camera's optical axis relative to the vehicle datum,
    and how repeatably can a replaced camera pod be surveyed back to it?
21. Is the forward fairing a sensor hood, a soft bumper, or a protective cage?
    The choice changes mass, airflow, and the prop-clearance target in §10.4.
22. Does the Cubie hold its 0–50 °C vendor range under a warm or solar soak
    inside the fairing while running the intended workload?
23. Does motor and prop vibration move the camera datum enough to matter,
    measured with motors stopped versus running and the camera installed?
24. Which independent pilot-view camera and link is used for manual flight, and
    are its latency and failure behavior verified separately from the tracking
    camera's?

## 16. Source-note references to verify

The source notes point to useful precedents. Their specific design conclusions
should be checked against the original papers before becoming requirements:

- [Towards Safe Mid-Air Drone Interception: Strategies for Tracking & Capture](https://arxiv.org/abs/2405.13542), published 2024;
- [Target Chase, Wall Building, and Fire Fighting: Autonomous UAVs of Team NimbRo at MBZIRC 2020](https://arxiv.org/abs/2201.03844), published 2022;
- the cited CTU Prague *Robotics and Autonomous Systems* pipeline, whose exact
  bibliographic entry still needs to be identified;
- any UZH latency result, MARSS claim, DroneClash claim, or field report before
  quoting it as quantitative evidence.

Vendor references already retained by the project include the Radxa Cubie A7Z
product, specification, MIPI CSI, and fan documentation. Exact purchased-board
revisions and camera-driver support still need verification.

## 17. Immediate next work

1. Review this document and mark any source-note idea that was summarized too
   aggressively.
2. Freeze the non-contact handoff packet and the maximum-state-age rule.
3. Continue the existing ground synthetic/wired milestone without adding drone
   dependencies.
4. In parallel, make the 5-inch/220 mm parametric CAD and live mass budget.
5. Select exact 6S motor/prop/ESC/battery candidates and collect manufacturer
   drawings; screen each combination through `tools/opencalc` (§11.4) and keep
   the stack file with the part list; do not cut final carbon yet.
6. Make an A1 Mini fit mule for the Cubie, camera, cooling, battery, and fairing.
7. Run thrust/current/sag/thermal and 3 mm-versus-5 mm arm coupon tests, and
   record the stand results in the tool's measured-CSV form so the modeled and
   measured curves can be compared directly.
8. Build and tune the manual data-collection vehicle before any autonomous
   pursuit/standoff test.
