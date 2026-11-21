# SITL frame files

ArduPilot SITL JSON frame files for `--model quad:<file>.json` (contract
V-F2, [S12]). `sitl.SitlInstance` copies the file into SITL's working
directory and passes the bare name. This 4.7.0 build panics on an absolute
path.

Every number here is **Provisional**. None is Measured. The file is strict
JSON, so the labels and sources live in this README.

## cone_x.json: the cone-tower X quad (V-F1, V-F6)

Key meanings were checked in `libraries/SITL/SIM_Frame.cpp`, `SIM_Frame.h`
and `SIM_Motor.cpp` at tag Copter-4.7.0 (read on the web, 2026-11-20).

| Key | Value | Meaning in SITL 4.7.0 | Source |
| --- | --- | --- | --- |
| `mass` | 0.64 kg | all-up mass | V-F6: CAD 0.527 kg + about 0.25 lb |
| `moment_inertia` | [2.71e-3, 2.69e-3, 2.41e-3] kg m^2 | body x, y, z inertia about the CoM | V-F6: CAD inertia scaled to 0.64 kg |
| `diagonal_size` | 0.105 m | arm length, center to motor (the code scales the template angle by it; it also scales yaw torque) | V-F6: 210 mm motor-to-motor diagonal / 2 |
| `motor1_position` .. `motor4_position` | (+-0.07425, +-0.07425, 0.019) m | motor position, body FRD from the CoM. A non-zero position replaces the template angle | X layout: 105 mm arm at 45 deg (149 mm side). z: prop plane 19 mm below the CoM (V-F6) |
| `num_motors` | 4 | checked against the template only | quad |
| `hoverThrOut` | 0.2 | thrust fraction at hover (`CTUN.ThO`); sets motor outflow speed so thrust at this command equals weight | V-F6: hover throttle about 0.2 |
| `propExpo` | 0.65 | thrust curve, `(1 - e) c + e c^2`; matches `MOT_THST_EXPO` in `copter.parm` | `copter.parm` |
| `disc_area` | 0.0527 m^2 | total true rotor disc area (momentum drag) | 4 x pi x (5.1 in / 2)^2, Gemfan 51433 |
| `mdrag_coef` | 0.10 | momentum drag ratio | as SITL's `freestyle.json` 5 in model |
| `refSpd`, `refAngle` | 14 m/s, 45 deg | body drag: the speed at which 45 deg of tilt holds level flight | estimate: frontal about 0.057 m^2, Cd 1 |
| `refVoltage`, `refCurrent` | 22.8 V, 4.9 A | hover electrical power (about 112 W) | momentum theory, figure of merit 0.5, motor+ESC 0.78 |
| `refAlt`, `refTempC` | 10 m, 25 C | air density reference | the SITL home altitude |
| `refBatRes` | 0.025 ohm | pack internal resistance | 6S at about 4 mOhm per cell |
| `refRotRate` | 500 deg/s | rotational air damping | guess, between SITL's defaults |
| `maxVoltage`, `battCapacityAh` | 25.2 V, 1.3 Ah | sim battery | 6S 1300 mAh |
| `pwmMin`, `pwmMax`, `spin_min`, `spin_max`, `slew_max` | 1000, 2000, 0.15, 0.95, 150 | ESC range and motor slew | ArduCopter defaults |

Not modeled: the 11 mm sideways CoM offset in CAD (V-F6 says to check it on
the vehicle), the tower's own drag torque, prop gyroscopic effects, and
anything about the camera (the frame file is physics only).

The airframe's defaults-file entries are in `environment.CONE_X`:
`FRAME_TYPE 1` (X mixer; `copter.parm` has 0, plus), `MOT_THST_HOVER 0.21`,
and the 6S `MOT_BAT_VOLT_MAX` / `MOT_BAT_VOLT_MIN`.

## Hover check (2026-11-20, SITL speedup 2, calm air)

GUIDED takeoff to 10 m, then 20 s of hover, then velocity setpoints from
the GCS link (`type_mask` 1479):

- `VFR_HUD.throttle` 20 % through the hover; mean motor PWM 1434 us.
- `MOT_THST_HOVER` learned 0.212, starting from the `copter.parm` 0.39.
  The airframe defaults set 0.21, as an operator would after a first hover.
- 2.0 m/s north: reached 1.99 m/s, pitch peak -16.7 deg.
- 5.0 m/s north: reached 5.04 m/s, steady pitch about -10.5 deg.
- In 6 m/s wind from the west (turbulence 1 m/s), it held position with
  a steady roll of about -12.7 deg. Hover learning stayed at 0.39: ArduCopter
  learns hover throttle only below about 5 deg of lean, so a hover in steady
  wind does not learn.

Finding: V-F6 gives thrust-to-weight 8-10 and hover throttle 0.2. In
ArduCopter's linearized thrust space these do not agree. Hover thrust is
about 1/(T/W), so T/W 8-10 gives about 0.10-0.125, which is at the
`MOT_THST_HOVER` floor of 0.125. 0.2 matches T/W 8-10 only as a raw motor
command (0.35 x 0.2 + 0.65 x 0.2^2 = 0.096). This file uses 0.2 as given,
so its T/W is 5. That is conservative for climb and acceleration. The
first real hover settles it.
