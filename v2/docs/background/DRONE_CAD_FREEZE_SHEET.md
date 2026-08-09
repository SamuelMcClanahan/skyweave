# Skyweave research quad: CAD freeze sheet

> **Background source material.** The core drone notes and the open-question
> register are in
> [`SKYWEAVE_DRONE_WORKING_DOCUMENT.md`](../SKYWEAVE_DRONE_WORKING_DOCUMENT.md).
> This file remains as detailed CAD background and source material.

**Revision:** 2026-07-22 preliminary layout

This is the sheet to use while building the first CAD assembly. It describes a
small, manually piloted research vehicle for sensing and data collection. The
forward part is a removable, rounded sensor fairing/soft bumper. It is not a
specification for a pointed rigid nose, collision guidance, or an impact
payload.

## The decision to make in CAD

Build one parametric true-X frame with two configurations:

- **5-inch primary:** enough room for the Cubie A7Z, cooling, battery, camera,
  and serviceable wiring;
- **4-inch alternate:** useful only if the weighed payload fits without
  sacrificing thrust, cooling, or battery retention.

Keep the motor geometry, camera datum, and electronics interfaces symmetric.
Do not make a deadcat or enclosed cinewhoop layout the first design. A deadcat
can keep propellers out of a forward camera view, but it introduces asymmetric
inertia and a different calibration/flight model. It can be a later variant if
the pilot camera actually sees propellers.

## Starting dimensions (parameters, not promises)

Use these values to make the CAD model usable now. Dimensions are nominal
length × width × height unless stated otherwise. Replace them with measured
part drawings before ordering carbon or printing a final shell.

| Parameter | 4-inch alternate | 5-inch primary | CAD rule |
| --- | ---: | ---: | --- |
| Propeller diameter | 101.6 mm (4.0 in) | 127.0 mm (5.0 in) | Define from the chosen prop, not the label |
| Motor-to-motor diagonal | 155–175 mm | 210–225 mm | Start at 165 / 220 mm |
| Arm width | 10–12 mm | 12–16 mm | Keep the motor pad locally wider |
| Structural plate | 3.0 mm carbon | 3.0 mm carbon | Do not start with a full 5 mm plate |
| Top deck | 2.0 mm G10 or carbon | 2.0–3.0 mm G10 or carbon | G10 is useful under electronics because it is insulating |
| Central payload deck placeholder | 80 × 40 mm | 100 × 50 mm | Keep the Cubie, camera, and battery inside this envelope where possible |
| FC/ESC mounting | 20 × 20 mm; adapter for 30.5 mm | 20 × 20 or 30.5 × 30.5 mm | Make both patterns configurable |
| Central stack/service height | 15–20 mm | 20–25 mm | Leave room for the selected boards, cable bends, and Cubie airflow |
| Motor pattern | 12 × 12, 16 × 16, or 19 × 16 mm | 16 × 16, 19 × 16, or 19 × 19 mm | Make hole pattern and screw size parameters |
| Battery placeholder envelope | 75–95 × 28–35 × 25–35 mm | 80–110 × 30–42 × 28–42 mm | Include lead bend, strap, and at least 10% measured clearance |
| Planning all-up mass band | 300–450 g | 450–600 g | Planning only; weigh every part |
| Thrust margin | 2:1 total static thrust:AUW floor; 2.5:1 target | 2:1 total static thrust:AUW floor; 2.5:1 target | Measure with the exact prop, pack, and payload |
| First battery chemistry | 4S LiPo | 4S LiPo | Capacity follows the measured mass/current budget |

The mass bands are not performance claims. The 4-inch configuration becomes
questionable quickly once the Cubie, heatsink, camera, buck converter, and
protective hardware are included. If the measured all-up mass cannot meet the
thrust and thermal gates, drop the 4-inch configuration rather than thinning
the safety-critical structure.

In this sheet, **motor-to-motor diagonal** means the distance between the
centres of opposite motors. In a symmetric true-X, adjacent motor spacing is
approximately `diagonal / sqrt(2)`. Draw the actual propeller discs in CAD and
check the gap between adjacent discs and the frame; the nominal prop diameter
and frame diagonal are not interchangeable measurements.

Create these as named master-sketch variables rather than scattered numbers:

```text
prop_diameter
motor_diagonal
arm_width, plate_thickness, arm_root_doubler_thickness
fc_pattern, motor_pattern, motor_screw_diameter
fc_stack_height, cubie_keepout, camera_datum_offset
battery_envelope, strap_slot_spacing, connector_bend_clearance
prop_to_shell_clearance, antenna_keepout, landing_clearance
```

## Payload growth path

Extra payload is a complete propulsion and airframe change, not just a motor
swap. The useful calculation is:

```text
maximum allowable all-up mass = measured total maximum thrust / chosen margin
payload allowance = that mass - dry vehicle mass - battery mass
```

Use a 2:1 total-thrust-to-weight ratio as an absolute starting floor and 2.5:1
as the preferred research-vehicle target. Recalculate it with the exact prop,
battery, motor, ESC, and payload. A larger battery can increase flight time,
but it also consumes part of the added payload allowance.

### If the extra payload is modest

Stay with the 5-inch frame only if the measured thrust stand and thermal tests
still pass. A different motor/prop combination may help, but it must be
matched as a system:

- larger stator motors add thrust but also add motor mass and arm loading;
- higher KV on the same prop can overcurrent the ESC and battery;
- a larger or higher-pitch prop may hit the frame, shell, or motor limits; and
- the battery, 5 V buck, wiring, and ESC must all survive the new current.

Do not bolt larger motors to the prototype arms without checking motor-hole
patterns, screw engagement, arm-root deflection, prop-disc clearance, ESC
current, and battery sag. Model the arm and motor pad as replaceable parts so a
motor upgrade produces a controlled revision rather than an improvised strap-on
adapter.

### If payload is a primary requirement

Move to a larger propeller class instead of trying to make a 5-inch quad do
everything. A 6-inch or 7-inch true-X configuration provides more propeller
disc area and usually carries a research payload more efficiently at lower
disc loading. That requires longer arms, a new shell, larger/lower-KV motors,
an appropriately rated ESC, a larger battery, and a new vibration/CG budget.

If redundancy becomes more important than simplicity, a hexacopter is another
option, but the extra motors, ESC channels, structure, and battery mass can
erase the benefit. It should be a separate vehicle configuration, not a late
addition to this frame.

The current CAD should therefore include a named `payload_mass` parameter,
adjustable battery space, replaceable arms, and a clear center payload deck,
but should not pretend that the 5-inch motor selection is expandable without a
new measured propulsion test.

## Short-duration, compact payload configuration

For a three- to four-minute test flight, it is reasonable to trade some
efficiency for higher available thrust, as long as the vehicle still hovers
well below maximum throttle and the battery/ESC temperatures are measured.
Do not size the system for three minutes of full-throttle operation.

The practical starting point is:

- keep the 5-inch configuration as the first build;
- use a high-thrust 5-inch prop/motor combination selected from a thrust stand,
  rather than choosing KV or pitch from a product listing;
- use a larger-diameter 6-inch configuration only if the measured 5-inch
  payload requires excessive hover throttle; and
- keep the battery large enough for voltage-sag and failsafe margin even if the
  flight objective is short.

Larger props usually improve lifting efficiency, but they also require more
motor torque, arm clearance, and ESC current. A high-pitch or tri-blade prop
can produce more static thrust in a compact package, but it normally costs
current and heat. The final choice must be measured with the actual battery and
payload.

### A1 mini fairing strategy

Assuming the stock A1 mini `180 × 180 × 180 mm` build envelope, keep every
printed part below about 170 mm in its largest dimension and verify the active
slicer profile. The carbon frame and propeller span do not need to fit inside
the printer; only the printed fairing, camera pod, feet, and cable guides do.

Use a lightweight central hood rather than a full printed prop shroud:

- the 5-inch central fairing should fit as one part if possible;
- a 6-inch fairing should be split into two or four bolted panels;
- use alignment keys and M2/M3 screws or captive nuts at the seams;
- keep the shell non-structural and removable;
- keep the shell at least 5 mm from the static prop disc, with 8–10 mm as the
  initial target until a flex/spinning test proves the margin; and
- keep the lens bracket rigid while allowing the TPU hood to flex or be
  replaced independently.

If the printed hood becomes too large, split it around a vertical or horizontal
plane and put the seam away from the camera window. Do not solve the printer
envelope by placing a thin printed guard close to the propeller tips; that adds
vibration and creates a failure mode at the highest-energy part of the vehicle.

## Recommended frame construction

Use a **modular sandwich frame** rather than a single printed shell carrying
the motors:

```text
top deck / payload bridge       2–3 mm G10 or carbon
electronics standoffs           M3, replaceable
bottom center plate             3 mm carbon
four bolt-on arms               3 mm carbon, replaceable
landing feet                    printed TPU, non-structural
fairing                         removable printed TPU, non-structural
```

The four arms and the central plates should carry flight loads through metal
fasteners and broad washers. The TPU fairing should only carry its own weight,
protect the camera, and manage airflow. Do not use printed snap fits or shell
adhesive as the primary motor or arm load path.

Use the flight controller's specified soft-mount hardware for its IMU when
appropriate. The ESC normally needs a deliberate rigid, thermally useful mount
and short, strain-relieved motor/battery wiring; do not put every board on the
same compliant TPU layer. The camera remains rigid to the frame, while the
Cubie may be isolated separately if vibration tests show that it helps.

### Why 3 mm is the starting thickness

Increasing a flat plate from 3 mm to 5 mm increases bending stiffness by about
`(5/3)^3 = 4.6`, but it also increases areal mass by about `5/3 = 1.67`.
Geometry, a top deck, and short replaceable arms often provide a better
stiffness-to-mass result than making every plate 5 mm. If testing shows that
the arm roots or camera bridge need more stiffness, test a **local 5 mm arm
root/bulkhead** or a second 3 mm doubler first. Do not buy a complete 5 mm
frame until a coupon, mass, and vibration comparison supports it.

### Frame alternatives and when they make sense

| Frame type | Use now? | Reason |
| --- | --- | --- |
| True-X modular sandwich | **Yes** | Symmetric inertia, simple calibration, replaceable arms, predictable FC model |
| Stretched-X | Maybe later | More room fore/aft, but changes inertia and shell proportions |
| Deadcat | Later | Better camera prop clearance, asymmetric dynamics and calibration |
| H frame | No for first build | More structure and drag for the same payload; less natural for a symmetric estimator |
| Ducted/cinewhoop | Bench/contained variant only | Guards add mass and drag; useful for safety testing, not the fast primary frame |
| Printed load-bearing frame | No | Creep, vibration, and uncertain fiber direction make it a poor first structural datum |

## Material schedule

| Part | Preferred material | Preliminary geometry | Notes |
| --- | --- | --- | --- |
| Arms and bottom plate | Known flat carbon laminate, preferably quasi-isotropic | 3.0 mm | Record layup, flatness, and supplied tolerance; cosmetic twill is not a strength specification |
| Top deck / electronics bridge | G10/FR4 or carbon | 2.0–3.0 mm | G10 provides electrical isolation below boards; carbon needs an insulating barrier |
| Local arm-root doubler | Carbon or G10 | 3.0 mm added locally, or 5.0 mm only after test | Avoid a blanket 5 mm plate until measured |
| Camera datum bracket | G10, carbon, or rigid PETG-CF with metal hardware | 2.5–3.0 mm | The optical datum must be rigid; do not mount the camera in flexible TPU |
| Cubie tray | PETG-CF, G10, or thin carbon with compliant pads | 2.0–3.0 mm | Leave connector, fan, and cable access; do not clamp the PCB hard |
| Fairing | 72D TPU or TPU-GF | 1.5–2.0 mm walls; 2.5–3.0 mm at mounts | Use ribs and a rounded profile instead of a solid nose |
| Vibration grommets / landing feet | Softer TPU (about 85A–95A) or silicone | As required by the selected hardware | 72D is a hard shell material, not a good soft isolator |
| Lens window | Optical-grade polycarbonate or glass | Replaceable, flat, non-printed | Calibrate with the window installed; omit it for the first exposed-lens test |
| Edge protection | Thin epoxy seal, heat-shrink, or molded edge guard | All exposed carbon edges | Seal after waterjetting; wet-finish carbon with appropriate PPE |

Confirm that the supplier really means **Shore D 72**, not a mislabeled 72A
product; Shore A and Shore D values are not interchangeable. TPU-GF generally
needs dry filament and a hardened nozzle. Its stiffness and
abrasiveness can be useful for a fairing, but it can be less forgiving than
unfilled TPU. Print a small hinge, boss, and grommet coupon before committing
to the shell. Keep a black shell out of direct sun during the first thermal
tests: the Cubie documentation gives a 0–50 °C operating recommendation, and
solar heating can consume that margin even when ambient air feels acceptable.
The vented first hood is not a weather-sealed enclosure; rain and water
ingress protection are later requirements, not an assumed property of TPU.

## Waterjet and carbon details

- Ask the supplier for the actual laminate construction, nominal thickness,
  flatness, and achievable hole/kerf tolerance. “Carbon fiber plate” alone is
  not enough information.
- Use rounded external corners and an inside radius of at least 3 mm at arm
  roots. Avoid sharp re-entrant corners that start delamination cracks.
- Keep a hole center at least about 2–3 hole diameters from a free edge until a
  supplier-specific stress check says otherwise. Use broad washers on carbon.
- Prefer through-holes with washers over countersinks. A countersink is a
  local laminate reduction and should be used only when the vendor has
  qualified it.
- Request a test coupon or one cheap fit plate before the production cut. Check
  the actual kerf, hole size, plate flatness, and whether standoffs sit square.
- Deburr and seal every exposed edge. Do not let a raw carbon edge touch a PCB,
  camera FPC, battery pouch, or operator hand.
- Treat carbon as conductive and RF-relevant. Add a G10/Kapton/nylon barrier,
  deliberate ground routing, and external receiver antennas where needed.

## Hardware and interfaces to reserve

### Structural hardware

- M3 high-strength steel socket-head screws for arm and plate joints, with
  washers and nylon-lock nuts or captive nuts;
- M3 7075-aluminium standoffs for the central stack, with nylon shoulder
  washers or a G10 isolation plate below electronics;
- M2/M2.5 hardware only where the selected FC, camera, or Cubie carrier
  specifies it;
- two non-collinear 2–3 mm stainless dowels or rigid inserted datum pins for
  the camera bracket (printed posts are fit-check only);
- 20 mm woven battery strap, an anti-slip battery pad, and a second strap on
  the 5-inch layout; and
- captive M3 nuts in the fairing mounts so repeated shell removal does not
  strip printed plastic.

Use medium threadlocker only on metal-to-metal joints and only after checking
compatibility with the fastener and finish. Do not put threadlocker,
cyanoacrylate, or uncured epoxy near the lens, FPC, TPU, or battery pouch.
Motor screw length must be checked against the actual motor: a screw that
bottoms in the
stator can destroy a motor even if the hole pattern is correct.

### Flight and power hardware

The minimum reproducible flight stack is:

```text
4 motors + matched propeller sets
4-in-1 ESC with documented voltage/current limits, DShot, telemetry, and current sensing
supported F7/H7 flight controller with blackbox, receiver failsafe, and spare UARTs
receiver and independent manual transmitter
low-latency pilot camera/VTX or another safe pilot-view link
dedicated 5 V companion buck, initially provisioned around 6 A (measure actual need)
Cubie A7Z + heatsink/fan + storage
global-shutter tracking camera + lens + cable/adapter
4S LiPo, battery lead, low-ESR capacitor, charger, and storage-safe case
buzzer, physical arming/kill procedure, and strain-relieved wiring
```

The tracking camera should not be assumed to be a safe pilot camera. A global-
shutter module may have a different field of view, latency, output format, or
failure behavior. Keep stabilization, arming, failsafe, and motor output on
the flight controller; the Cubie is a companion computer until a separately
tested high-level command path exists.

Reserve a 20 × 20 mm and 30.5 × 30.5 mm FC/ESC pattern in the master sketch.
For the motor pads, parameterize 12 × 12, 16 × 16, 19 × 16, and 19 × 19 mm
patterns and both M2 and M3 clearance holes. Also leave a parameter for the
motor's prop interface (T-mount versus a shaft and nut). This prevents a cheap but
undocumented Taobao board or motor from forcing a new frame cut.

## What to buy and measure before the final cut

The following is the minimum reproducible hardware set. Pick one actual part
for each line, record its revision and mass, and put its dimensions into the
CAD master before freezing holes:

```text
4 x motors with verified mounting pattern, prop interface, 4S current, and thrust data
1 x 4-in-1 ESC with documented 4S rating, DShot, telemetry, and current sensing
1 x F7/H7 flight controller with supported firmware target and receiver failsafe
1 x receiver, independent transmitter/arming/kill path, and buzzer
1 x low-latency pilot camera/VTX or another independent pilot-view link
1 x current-limited 5 V companion buck, initially reserved at about 6 A
1 x Cubie A7Z, heatsink/fan, storage, and camera interface hardware
1 x global-shutter tracking camera, lens, rigid bracket, and lens window
1 x 4S LiPo, charger, storage case, connector, capacitor, and battery straps
several propeller sets, verified motor screws, spare arms, and structural hardware
smoke stopper/current limiter and a contained test cage or prop guards
```

The tracking camera is not automatically a safe pilot camera. Keep a separate
pilot-view path unless latency, field of view, and failure behavior have been
measured and accepted for manual flight.

## Fairing and camera construction

The fairing should be a four-point removable hood around a rigid camera pod:

```text
frame datum -> rigid camera bracket -> lens/window -> TPU fairing
                                         \\-> Cubie tray and airflow path
```

Recommended details:

- camera optical center close to the vehicle centerline and near the intended
  reference plane;
- two repeatable datum features plus a clamp, not a flexible printed hinge;
- a replaceable lens window with a flat seating surface;
- no printed transparent lens surface in the calibrated optical path;
- an opening or rounded cover with no hard point extending forward;
- shell mounts isolated from the camera datum so shell flex cannot change the
  measured pose;
- inlet/outlet vents around the Cubie heatsink, with a removable fan path; and
- a sacrificial soft bumper or cage for contained tests, attached to the shell
  rather than the camera lens.

Keep at least 5 mm of static clearance from the full propeller disc to the
fairing, antennas, and wiring as a minimum placeholder; target 8–10 mm until a
spinning/flex test demonstrates the real margin. The final clearance is set by
the actual prop geometry, arm deflection, and shell flex, not by the nominal
prop diameter.

## CAD work order

### Lock now

1. Coordinate system: X forward, Y right, Z up; origin at the intended CG.
2. True-X motor-center layout with 4-inch and 5-inch configurations.
3. 3 mm structural carbon as the default; modular arms and a removable top
   deck.
4. 20 × 20 and 30.5 × 30.5 electronics patterns, plus the configurable motor
   patterns.
5. Battery strap slots, Cubie keep-out, camera datum, antenna keep-outs, and
   a non-load-bearing fairing interface.
6. Prop-disc and service clearances, cable routes, and access direction.

The first cut does not have to be the final carbon cut. After the 1:1 fit mule,
make a low-cost prototype plate/arm set (for example, G10 or inexpensive
3 mm carbon) so the bench and contained tests can happen on real structure.
Keep the final laminate, thickness, and arm-root geometry provisional until
those measurements are recorded.

### Figure out before final waterjet or flight hardware

1. Weigh the exact Cubie RAM/storage variant, heatsink/fan, camera, lens,
   selected FC, ESC, motors, battery, shell, and wiring.
2. Obtain vendor drawings for the actual FC/ESC, motor, prop, battery, camera
   module, connector, and buck converter. Measure anything undocumented.
3. Select a motor/prop/4S combination from thrust-stand current and temperature
   data. Check total maximum thrust against measured all-up mass; 2:1 is a
   starting floor, not a waiver for a heavy payload.
4. Check the 5 V rail at compute/camera peak load, buck temperature, Cubie
   temperature, and shell solar/airflow cases.
5. Check CG, battery adjustment range, arm-root stiffness, prop balance, and
   vibration with the camera installed.
6. Verify the FC firmware target, UART voltage levels, receiver protocol,
   failsafe behavior, and the independent pilot-view link.
7. After the prototype bench/contained tests, update the mass, stiffness,
   thermal, and clearance values before ordering the final waterjet revision.

For a thickness decision, make a coupon using the same span, cutout, fastener
spacing, and laminate as the proposed arm. Record thickness, mass, applied
load, deflection, and the first visible resonance. That result is more useful
than a generic “3 mm versus 5 mm” claim because the arm cutout, laminate layup,
motor mass, and fastener preload determine the real mode shape.

### Done when

- the assembled CAD mass properties are within the measured budget with a
  documented growth margin;
- the battery can move enough to trim CG without moving the camera datum;
- every board, connector, fan, antenna, and fastener can be installed and
  removed without dismantling the arms;
- no prop disc intersects the shell or wiring in the static and measured-flex
  checks;
- the camera pod can be removed and reinstalled with a measured repeatability;
- the shell has an airflow/thermal test plan and no hard forward protrusion;
- a 1:1 fit print passes before carbon is cut; and
- the bench, restrained-motor, and contained-flight safety gates in the main
  requirements document are satisfied.

## Risks to test explicitly

| Risk | First check |
| --- | --- |
| Carbon delamination, crush, short, or RF shielding | Inspect/seal edges, use insulating films, check electrical continuity, and keep antennas off the carbon plane |
| Motor/prop vibration moving the camera datum | Balance props, log FC gyro data, and compare camera-pod deflection with motors stopped/running |
| Cubie thermal or solar overload | Run the actual workload with a fan, temperature logging, and a warm/solar soak before enclosing it |
| LiPo fault, connector heating, or battery shift | Use a smoke stopper/current limit, secure the pack with two straps, measure sag/temperature, and verify low-voltage failsafe |
| FC/radio/companion failure | Verify an independent RC kill/disarm path and FC-only stabilization with the Cubie unplugged |
| Manufacturing and service drift | Use a fit mule, serialize the as-built mass/CG/optical datum, and record every replacement pod/arm |

## Things not to freeze yet

Do not hard-code the final motor KV, prop pitch, LiPo capacity, exact camera
module, shell vent size, or FC pinout from a product listing. Those values are
dependent on measured mass, current, thermal behavior, and the exact vendor
part. Keep them as named CAD parameters until the parts are in hand.
