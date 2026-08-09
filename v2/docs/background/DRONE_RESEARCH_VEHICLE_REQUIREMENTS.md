# Skyweave drone research vehicle requirements

> **Background source material.** The core drone notes and the open-question
> register are in
> [`SKYWEAVE_DRONE_WORKING_DOCUMENT.md`](../SKYWEAVE_DRONE_WORKING_DOCUMENT.md).
> This file remains as detailed background and source material.

**Status:** preliminary CAD and integration brief
**Purpose:** a safe flying research vehicle for onboard tracking experiments

## Scope and safety boundary

This brief covers the airframe, flight electronics, companion computer,
camera, power system, cooling, and safe test hardware.

It does not specify a rigid impactor, pointed kinetic nose, collision guidance,
or an impact payload. The forward body described here is a rounded, removable
sensor fairing or soft bumper for non-contact tracking tests. Any future
interception research should use a non-contact or soft-capture test article
with a separately reviewed safety plan.

## Recommended starting configuration

Use a **5-inch X-frame** as the default CAD configuration.

The Cubie A7Z is only 65 x 30 mm, but the complete payload also needs a
heatsink or airflow, camera board and lens, wiring, radio hardware, battery,
and a protective fairing. A 4-inch frame can work only if the measured
all-up mass stays low and the payload is kept compact.

Create both 4-inch and 5-inch configurations in CAD, but design the first
assembly around the 5-inch envelope. Do not make the airframe fast or
high-energy while the sensing and control stack is still being validated.

Initial flight targets:

```text
vehicle type:        4-motor X quadcopter
configuration:       5-inch primary, 4-inch alternate
first battery:       4S LiPo
control:             independent flight controller owns stabilization/failsafe
companion:           Cubie A7Z owns camera processing and experiment software
camera:              global-shutter camera, fixed lens, forward-facing
test nose:           rounded removable TPU sensor fairing, not an impactor
test environment:    tethered/contained, low altitude, clear propeller area
```

## Cubie A7Z constraints to design around

Radxa's current documentation lists:

- 65 x 30 mm board size;
- Allwinner A733, 2 Cortex-A76 plus 6 Cortex-A55 cores;
- 3 TOPS INT8 NPU;
- LPDDR4/4x options up to 16 GB;
- one 4-lane MIPI CSI interface;
- USB-C 2.0 OTG/power and USB-C 3.0 OTG/DisplayPort;
- standard 40-pin GPIO with UART, SPI, I2C, and PWM;
- a 5 V fan connector;
- 5 V USB-C or GPIO power; and
- recommended operating temperature of 0-50 C.

The board should not be powered directly from the flight battery. Reserve a
dedicated regulated 5 V rail and measure the actual peak current under camera,
storage, NPU, Wi-Fi, and recording load. Provisionally design the rail for
25-30 W until the real board and software profile say otherwise.

The official MIPI camera documentation lists Radxa's 4K, 8M 219, and 13M 214
cameras. An OV9281 may work through a compatible module and driver, but that is
not established by the official supported-camera list. The CAD must therefore
support:

1. the preferred OV9281 MIPI module if its driver, FPC, and power rails are
   confirmed; and
2. a USB/UVC global-shutter camera fallback without redesigning the frame.

Leave access to the microSD/UFS path, antenna connector, USB ports, fan
connector, camera connector, and U-Boot button. Do not bury the board in a
fully sealed, thermally insulating cone.

Official references:

- [Radxa Cubie A7Z product page](https://radxa.com/products/cubie/a7z/)
- [Cubie A7Z specifications](https://docs.radxa.com/en/cubie/a7z)
- [Cubie A7Z MIPI CSI documentation](https://docs.radxa.com/en/cubie/a7z/hardware-use/mipi-csi)
- [Cubie A7Z fan interface](https://docs.radxa.com/en/cubie/a7z/hardware-use/fan)

## CAD master layout

Use the companion [CAD freeze sheet](./DRONE_CAD_FREEZE_SHEET.md) while
building the first assembly. It contains the provisional 4-inch/5-inch
parameters, material and thickness schedule, frame tradeoffs, hardware
interfaces, and the measurements that must be completed before a final
waterjet cut. The values below remain requirements; the freeze sheet is the
more detailed, editable starting point.

Create a master layout sketch before modeling the shell. Use this coordinate
convention:

```text
X: forward, along the camera optical axis
Y: right when looking forward
Z: up
origin: vehicle center of gravity target
```

The master layout should contain configurable parameters for:

- prop diameter and prop-disc clearance;
- motor-to-motor diagonal and arm width;
- bottom and top plate thickness;
- motor hole pattern;
- FC/ESC hole pattern and stack height;
- Cubie 65 x 30 mm keep-out plus connector clearance;
- camera board, lens diameter, lens protrusion, and FPC bend radius;
- battery length, width, height, and strap path;
- antenna and receiver keep-outs;
- shell wall thickness and ventilation openings;
- landing clearance; and
- target center of gravity and payload envelope.

Make the camera mount and Cubie tray replaceable parts. Do not make the
camera lens position depend on a one-off printed shell.

## Carbon-fiber frame

### Recommended construction

Start with a modular true-X sandwich: 3 mm waterjet carbon for the lower
plate and replaceable arms, plus a 2–3 mm G10 or carbon upper deck. Use M3
through-fasteners and broad washers for the load path. Treat 5 mm material as
an optional local arm-root or central reinforcement only after a coupon, mass,
and vibration check. A full 5 mm 4- or 5-inch frame is likely to add mass
without helping the sensing experiment.

Use:

- rounded external corners;
- generous internal radii around slots and cutouts;
- washers under fastener heads;
- no carbon edge touching exposed electronics or solder joints;
- insulating film or nylon standoffs under boards; and
- sealed or covered carbon edges where the operator can touch them.

Carbon fiber is electrically conductive. The frame must not become an
unintended ground, antenna shield, or short across a power board.

### Structural requirements

- Keep the flight controller near the vehicle CG.
- Keep the battery position adjustable so the final CG can be tuned.
- Keep the Cubie near the CG rather than at the extreme nose.
- Rigidly mount the camera relative to the frame; isolate the companion
  computer from high-frequency motor vibration if necessary.
- Leave a clear propeller disc and motor cooling path.
- Use captive nuts or accessible screws so the top shell can be removed
  without removing the arms.
- Design landing loads and ordinary crashes first; do not design for impact
  loading.

## Forward fairing and camera pod

The forward cover should be a removable aerodynamic and protective fairing,
not a pointed collision part.

Recommended construction:

- thin-wall 72D TPU or TPU-GF shell, with ribs rather than a solid mass;
- softer TPU grommets or inserts between the shell and electronics;
- rounded nose radius and no hard protruding tip;
- clear, replaceable lens window with a controlled optical surface;
- generous ventilation behind and above the Cubie heatsink;
- separate camera bay so lens changes do not disturb board mounting;
- quick-release or sacrificial shell sections for ordinary test damage.

TPU-GF may be stiffer but can be less forgiving and more abrasive than normal
TPU. Use it for the fairing only if the print process and flex behavior are
acceptable. Do not use a flexible camera mount: the camera must not move
relative to the frame during a measurement.

The camera optical center should be close to the vehicle centerline and its
orientation should be represented by a replaceable, measurable bracket. Add
two dowel/reference features so a replacement camera pod can be surveyed back
to the same datum.

## Flight controller and motor-control stack

### Flight controller

Select a supported F7 or H7 flight controller with:

- 2-6S input or a clearly documented regulated input path;
- independent IMU and barometer where available;
- current and voltage sensing;
- at least two spare UARTs after receiver and companion-computer links;
- blackbox logging;
- DShot support;
- a real failsafe and arming switch; and
- a known firmware target.

The cheap Taobao board is acceptable only after confirming its MCU, firmware
target, UART pinout, IMU, regulator limits, and current-sensor behavior. A
random board that cannot be reproduced or reflashed is a poor foundation for
the companion-computer experiment.

Use this ownership split:

```text
flight controller: attitude stabilization, motor output, arming, failsafe
Cubie A7Z:         camera capture, tracking, logging, high-level experiments
```

The Cubie should not directly generate motor PWM during the first flight
tests. The FC must be able to land/disarm or hold a safe state if the Cubie
reboots, overheats, or loses its link.

Reserve an independent receiver/transmitter, buzzer, physical kill/disarm
procedure, and low-latency pilot-view camera/link. The global-shutter tracking
camera is an experiment sensor, not automatically a safe pilot camera; its
latency, field of view, and failure behavior must be measured separately.

Betaflight is a reasonable manual-flight baseline. ArduPilot or PX4 is more
appropriate if the companion computer will later send high-level navigation
commands, but it requires a genuinely supported FC rather than an arbitrary
clone.

### ESC

Use a 4-in-1 ESC with:

- voltage range matching the selected battery;
- continuous current margin above measured motor demand;
- DShot and telemetry support;
- current sensing or a known FC current-sensor path; and
- a separate, documented logic/BEC supply.

Do not rely on an ESC's small 5 V regulator to power the Cubie. Use the
dedicated companion-computer buck converter.

### Motors and propellers

Choose motors only after the all-up mass and prop clearance are fixed. A safe
starting envelope is:

```text
5 inch: 2207/2306 class, roughly 1700-2000 KV on 4S
4 inch: 2004/2204 class, roughly 2500-3000 KV on 4S
props:  low-to-moderate pitch for initial contained tests
```

These are starting ranges, not a final shopping list. Validate with a thrust
stand and keep a minimum total maximum-thrust-to-weight ratio of 2:1; 2.5:1
gives more control margin for a loaded research vehicle. Do not tune for
maximum speed while the payload, thermal, and tracking stack is unproven.

## Power system

Use separate, filtered power domains:

```text
LiPo
  -> 4-in-1 ESC / motors
  -> flight-controller regulated rail
  -> dedicated 5 V high-current buck -> Cubie A7Z
  -> camera rail as required by the selected module
```

Include:

- low-ESR capacitor at the ESC/battery input;
- fused or current-limited companion rail;
- measured 5 V voltage/current at the Cubie;
- common ground with deliberate routing;
- twisted or filtered camera/companion power wiring;
- XT30 for a genuinely small 4-inch build or XT60 for a larger 5-inch build;
- battery strap and strain relief; and
- an accessible physical power/arming procedure.

Start with a 4S pack rather than adding 6S complexity. A rough starting range
is 850-1300 mAh for a 5-inch research vehicle, but select the final capacity
from measured hover current, desired test duration, and mass budget. Marketing
C-ratings are not a substitute for voltage-sag measurements.

Required bench equipment includes a smoke stopper/current limiter, LiPo-safe
charger, storage-charge capability, cell voltage monitoring, and a safe battery
storage/transport procedure.

## Camera and onboard tracking computer

### Camera requirements

- global shutter;
- fixed-focus, fixed-lens mount;
- documented resolution and frame rate;
- manual exposure/gain control;
- timestamp access or a documented Cubie capture timestamp;
- rigid optical datum;
- lens window that can be calibrated in place; and
- MIPI and USB mounting provisions until OV9281 driver support is confirmed.

An OV9281 module is a reasonable candidate for a small monochrome global-
shutter tracker, but verify the exact module's connector, voltage rails,
driver, pixel format, frame rate, and lens before freezing the camera bay.
Radxa's official supported-camera page does not establish OV9281 support.

### Cubie software/data path

```text
global-shutter camera -> Cubie capture/detector
flight controller      -> Cubie attitude, battery, and flight state
Cubie                  -> recorder/tracker and later high-level telemetry
```

Timestamp the camera frames with the Cubie's monotonic clock and retain the FC
time/attitude fields. Do not claim camera/FC synchronization until it is
measured. Keep recorded frames and runtime logs on removable or onboard
storage so a failed radio link does not erase the experiment.

## Mass and center-of-gravity budget

Create a live mass budget before choosing plate thickness or battery. Include:

```text
frame plates, arms, standoffs, and fasteners
motors and propellers
FC, 4-in-1 ESC, receiver, buzzer, and wiring
LiPo and connector
Cubie A7Z, heatsink/fan, storage, antenna, and mount
camera, lens, FPC/USB cable, and camera pod
TPU fairing and lens window
10-15% growth margin
```

The target is not a particular gram number yet. The design is acceptable when
the measured all-up mass still gives the required thrust margin, the CG is
within a few millimetres of the intended origin, and the battery can move
fore/aft without changing the camera datum.

Put the battery on adjustable rails or a strap path. Put the Cubie and camera
near the center of mass. A top-mounted camera is fine, but account for its
lever arm and keep the lens bracket rigid.

## CAD deliverables

Model these as separate parts/configurations:

1. 4-inch and 5-inch carbon plate layouts;
2. motor/arm plates and replaceable standoffs;
3. FC/ESC adapter plate;
4. adjustable battery tray and strap slots;
5. Cubie A7Z clamp/tray with connector keep-outs;
6. camera bracket with a repeatable optical datum;
7. heatsink/fan and airflow clearance;
8. removable TPU fairing/soft bumper;
9. antenna/receiver mounts;
10. landing feet and safe prop-clearance guards for early testing; and
11. a master assembly showing CG, mass properties, and cable paths.

Before waterjetting, export a 1:1 paper or cheap-printed fit check for the
Cubie, FC, ESC, camera, lens, battery, connectors, and fasteners. Verify the
actual Cubie board drawing and camera module dimensions rather than relying on
the nominal 65 x 30 mm outline alone.

## Test gates before flight

### Bench gate

- verify every voltage rail under maximum compute and camera load;
- run the Cubie with heatsink/fan and log temperature;
- verify FC arming, receiver failsafe, current sensing, and motor direction;
- run the camera and recorder without motors;
- check that carbon cannot short any board or connector; and
- perform a restrained motor/prop test with a smoke stopper.

### Tethered/contained gate

- use prop guards or a test cage;
- start with the soft fairing installed and no hard forward payload;
- test hover stability and vibration;
- record camera frames while motors run;
- compare Cubie timestamps, FC telemetry, and camera frame age; and
- verify that loss of the Cubie does not create an unsafe motor command.

### Free-flight research gate

Only after the previous gates pass, test low-altitude manual flight in a
controlled area with a physical kill method, conservative battery limits, and
no collision objective. The vehicle should first be a tracking-data collector,
not an interceptor.

## Decisions still needed before freezing CAD

1. 4-inch or 5-inch primary configuration; the recommendation is 5-inch.
2. Actual all-up mass target and allowable companion payload mass.
3. Exact Cubie RAM/storage variant and heatsink/fan arrangement.
4. OV9281 MIPI module and driver, or USB/UVC fallback.
5. FC firmware target: manual-flight Betaflight versus supported ArduPilot/PX4.
6. Motor/prop/battery combination after a thrust and mass estimate.
7. Encoder hardware is not a first-drone dependency; if a later turret uses
   one, confirm its actual part number in that separate BOM.
8. Battery connector and charging/storage procedure.
9. Camera lens, optical axis, and repeatable calibration datum.
10. Whether the fairing is a sensor hood, soft bumper, or protective cage.

Do not freeze the final shell or waterjet plate until the selected hardware,
camera datum, and fairing interface have a measured mass, power, connector,
thermal, and clearance budget. A low-cost prototype cut may be used for those
measurements first.
