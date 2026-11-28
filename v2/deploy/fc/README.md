# SkyWeave FC parameter baseline (contract B-D4)

This folder holds the ArduCopter parameter baseline for the SkyWeave cone X
quad. The operator loads it with a ground station. The companion software
never writes a parameter (contract [F1]).

**Every value here is Provisional. No value is Measured.** The file was
built on 2026-11-27 from the ArduPilot documentation, the contract and the
V-F6 airframe numbers. It was not loaded on the real flight controller (FC).

| File | Content |
| --- | --- |
| `skyweave_cone_x.param` | 127 parameters, `NAME,VALUE`, in 15 groups |
| `README.md` | This file: the reason and the source of each value |
| `check_params.py` | The checks: project rules, ArduPilot metadata, SITL read-back |

Read section 2 before you connect a battery.

## 1. What the file assumes

All of these are Provisional. They come from the owner, the CAD (V-F6) and
the E1 brief.

| Item | Assumed |
| --- | --- |
| FC | Matek H743-SLIM, firmware target `MatekH743` (not `MatekH743-bdshot`), ArduCopter 4.7.x |
| Board version | The owner says V3. The E1 brief §1 says V4. Section 2 has a check for this |
| Frame | X quad, 210 mm motor diagonal, about 0.64 kg, top-heavy cone tower |
| Motors, propellers | T-Motor Velox V2207 1750KV, Gemfan 51433 (5.1 in, 3 blades) |
| ESC | 4-in-1, AM32 firmware, 55 A. DShot300. No bidirectional DShot, no ESC telemetry |
| Battery | 6S LiPo, 1300 mAh |
| Companion | SERIAL1 (UART7), MAVLink2, 921600 baud, 3 wires (no RTS/CTS) |
| GPS | u-blox NEO-M9N on SERIAL3 (USART2, pads TX2/RX2). The wiring is not recorded |
| Compass | External QMC5883L board (GY-271) on I2C. The orientation is not known |
| Receiver | ExpressLRS, CRSF, on SERIAL7 (USART6, pads TX6/RX6). The wiring is not recorded |
| Hover throttle | About 0.2 (estimate). Thrust is not measured |

ArduCopter 4.7 changed many parameter names and units. The brief and older
guides use the old names. This file uses the 4.7 names.

| Old name (4.6 and before) | 4.7 name | Change |
| --- | --- | --- |
| `ANGLE_MAX` (centidegrees) | `ATC_ANGLE_MAX` | degrees |
| `ARMING_CHECK` | `ARMING_SKIPCHK` | The checks to skip. 0 = all checks on |
| `RTL_ALT`, `RTL_SPEED` (cm) | `RTL_ALT_M`, `RTL_SPEED_MS` | m, m/s |
| `LAND_SPEED` (cm/s) | `LAND_SPD_MS` | m/s |
| `WPNAV_SPEED`, `WPNAV_ACCEL` (cm) | `WP_SPD`, `WP_ACC` | m/s, m/s² |
| `PILOT_SPEED_UP` (cm/s) | `PILOT_SPD_UP` | m/s |
| `ATC_ACCEL_P_MAX` (centideg/s²) | `ATC_ACC_P_MAX` | deg/s² |
| `PSC_ACCZ_P`, `PSC_ACCZ_I` | `PSC_D_ACC_P`, `PSC_D_ACC_I` | 0.1 times the old value |
| `PSC_JERK_XY` | `PSC_JERK_NE` | name only |
| `GPS_TYPE`, `GPS_GNSS_MODE` | `GPS1_TYPE`, `GPS1_GNSS_MODE` | name only |
| `SRn_*` stream rates | `MAVn_*` | name only |

## 2. Check before first power-on

These values depend on the wiring or on a bench measurement. The file holds
a safe default for each. Check each line. Change the value if the vehicle
is different.

1. **Board version and firmware.** Flash `MatekH743` for Copter 4.7.x. After
   the first start, make sure that Mission Planner shows two IMUs and a
   barometer with no "hardware" pre-arm message. Reseller pages say that
   the V4 board has different sensors from the V3 board (two ICM42688P, a
   DPS368 barometer) and the same pinout. The firmware source at tag
   Copter-4.7.0 names boards up to V3 (HWDEF). No check was done on a real
   board.
2. **Receiver wiring (`BRD_ALT_CONFIG`, `SERIAL7_PROTOCOL`).** The file
   expects the ExpressLRS receiver on pads TX6 and RX6. Receiver TX goes to
   RX6. Receiver RX goes to TX6. If the receiver is on a different UART, set
   `SERIALn_PROTOCOL` 23 on that port, set `SERIAL7_PROTOCOL` -1 and set
   `BRD_ALT_CONFIG` 0.
   USART6 has no DMA in the `MatekH743` firmware (HWDEF). The ArduPilot
   CRSF page says that DMA is necessary on F4 and F7 boards only; this
   board is an H7. If the RC link is not reliable, move the receiver to a
   UART with DMA.
3. **GPS wiring (`SERIAL3_PROTOCOL`).** The file expects the GPS on pads TX2
   and RX2 (USART2). If the GPS is on TX3/RX3 (USART3, SERIAL4), set
   `SERIAL3_PROTOCOL` -1. Then SERIAL4, a GPS port by default, is the
   first GPS port.
4. **Companion wiring (`SERIAL1_*`, `BRD_SER1_RTSCTS`).** The file expects
   3 wires on UART7 (TX7, RX7, GND). If RTS and CTS are connected, set
   `BRD_SER1_RTSCTS` 1.
5. **Transmitter channels (`RCMAP_*`, `FLTMODE_CH`, `RC7_OPTION`).** Set the
   transmitter to this channel order:

   | Channel | Function | Radio control |
   | --- | --- | --- |
   | 1, 2, 3, 4 | Roll, pitch, throttle, yaw (AETR) | Sticks |
   | 5 | ExpressLRS arm channel. No function in the FC | 2-position switch |
   | 6 | Flight mode: ALT_HOLD / GUIDED / RTL | 3-position switch |
   | 7 | Motor emergency stop | Guarded momentary switch (EdgeTX logical switch, hold 0.5 s) |
   | 8 | Approve, read by the companion ([F7]) | 2-position or momentary switch |

   ExpressLRS sends channel 5 as a 2-position channel in its Hybrid and
   Wide switch modes. Thus the 3-position mode switch cannot be on
   channel 5. Do not move the mode switch to channel 5.
6. **Compass orientation (`COMPASS_ORIENT`).** The file holds 0 (no
   rotation) as a placeholder. `COMPASS_AUTO_ROT` 2 lets the compass
   calibration find and write the orientation. After the calibration, read
   `COMPASS_ORIENT` and record it. Then check the heading against a known
   direction.
7. **Battery voltage scale (`BATT_VOLT_MULT`).** Compare the voltage in
   Mission Planner with a multimeter on the battery. The file holds 11.0,
   the firmware default for this board. The ArduPilot board page lists 10.5.
   If the true value is 10.5, the FC reads 4.8 % high with 11.0, and the
   battery failsafes come late. Do this check before a flight.
8. **Battery current scale (`BATT_AMP_PERVLT`, `BATT_MONITOR`).** The
   current signal comes from the 4-in-1 ESC. The file holds 40.0, the board
   default, which is not a value for this ESC. Get the scale from the ESC
   data, or calibrate it against a charger (mAh put back after a flight).
   If the ESC current pad is not connected, set `BATT_MONITOR` 3 (voltage
   only) and set `BATT_LOW_MAH` and `BATT_CRT_MAH` to 0. The battery
   percent that the mission reads (T17, `battery_floor_pct`) is wrong until
   this scale is correct.
9. **Motor order and spin direction (`FRAME_TYPE`, `SERVOn_FUNCTION`).**
   The file holds ArduPilot X (`FRAME_TYPE` 1): output 1 = front right,
   counter-clockwise; output 2 = rear left, counter-clockwise; output 3 =
   front left, clockwise; output 4 = rear right, clockwise (seen from
   above). Most 4-in-1 ESCs use the Betaflight order (1 = rear right,
   2 = front right, 3 = rear left, 4 = front left). If the ESC is plugged
   straight into the FC, `FRAME_TYPE` 12 (BetaFlightX) is the usual
   correction. Use the motor test (section 7, step 9) to find the true
   order. Change the direction of a motor in the AM32 configuration tool, or
   exchange two motor wires.
10. **Motor spin limits (`MOT_SPIN_ARM`, `MOT_SPIN_MIN`).** The file holds
    the ArduPilot defaults (0.10, 0.15). Measure the lowest reliable spin
    with the motor test, as the ArduPilot page "Setting motor range" tells
    you. This vehicle has much thrust for its weight, so a high
    `MOT_SPIN_MIN` removes much of the control range below hover.
11. **Emergency stop switch (`RC7_OPTION` 31).** Make sure that the switch
    is low (below 1200 us) when it is released. The FC refuses to arm while
    the switch is high.
12. **SD card.** Put a card in the FC. With `ARMING_SKIPCHK` 0 the FC
    refuses to arm when it cannot write a log.

## 3. The parameters

Columns: the value in the file, the reason, and the source. "Default" in the
Why column means that the value is the ArduCopter 4.7.0 default, and that
the file holds it so that an old value on the FC cannot stay. "CHECK" points
to section 2.

Source keys:

| Key | Source |
| --- | --- |
| PARAM | ArduCopter 4.7.0 parameter list: <https://ardupilot.org/copter/docs/parameters-Copter-stable-V4.7.0.html> |
| BOARD | Matek H743 board page: <https://ardupilot.org/copter/docs/common-matekh743-wing.html> |
| HWDEF | Board definition at tag Copter-4.7.0: <https://github.com/ArduPilot/ardupilot/blob/Copter-4.7.0/libraries/AP_HAL_ChibiOS/hwdef/MatekH743/hwdef.dat> |
| TUNE | "Setting the aircraft up for tuning": <https://ardupilot.org/copter/docs/setting-up-for-tuning.html> |
| MPCALC | Mission Planner "Initial Parameter Setup" formulas: <https://github.com/ArduPilot/MissionPlanner/blob/master/GCSViews/ConfigurationView/ConfigInitialParams.cs> |
| MOTRANGE | "Setting motor range": <https://ardupilot.org/copter/docs/set-motor-range.html> |
| MOTORS | "Connect ESCs and motors" (order, direction, motor test): <https://ardupilot.org/copter/docs/connect-escs-and-motors.html> |
| DSHOT | "DShot ESCs": <https://ardupilot.org/copter/docs/common-dshot-escs.html> |
| CRSF | "TBS Crossfire and ExpressLRS": <https://ardupilot.org/copter/docs/common-tbs-rc.html> |
| ELRS | ExpressLRS switch modes: <https://www.expresslrs.org/software/switch-config/> |
| AUX | "Auxiliary functions": <https://ardupilot.org/copter/docs/common-auxiliary-functions.html> |
| MODES | "RC transmitter flight mode configuration": <https://ardupilot.org/copter/docs/common-rc-transmitter-flight-mode-configuration.html> |
| FS-RC | "Radio failsafe": <https://ardupilot.org/copter/docs/radio-failsafe.html> |
| FS-BATT | "Battery failsafe": <https://ardupilot.org/copter/docs/failsafe-battery.html> |
| FS-GCS | "GCS failsafe": <https://ardupilot.org/copter/docs/gcs-failsafe.html> |
| FS-EKF | "EKF failsafe": <https://ardupilot.org/copter/docs/common-ekf-inav-failsafe.html> |
| CRASH | "Crash check": <https://ardupilot.org/copter/docs/crash_check.html> |
| FENCE | "Cylindrical fence": <https://ardupilot.org/copter/docs/common-ac2_simple_geofence.html> |
| RTL, LAND, GUIDED | <https://ardupilot.org/copter/docs/rtl-mode.html>, <https://ardupilot.org/copter/docs/land-mode.html>, <https://ardupilot.org/copter/docs/ac2_guidedmode.html> |
| PREARM | "Pre-arm safety checks": <https://ardupilot.org/copter/docs/common-prearm-safety-checks.html> |
| COMPASS | "Compass calibration": <https://ardupilot.org/copter/docs/common-compass-calibration-in-mission-planner.html> |
| EKFSRC | "EKF source selection": <https://ardupilot.org/copter/docs/common-ekf-sources.html> |
| NOCOMPASS | "Compass-less operation" (GSF yaw): <https://ardupilot.org/copter/docs/common-compassless.html> |
| BATCH | "IMU batch sampling": <https://ardupilot.org/copter/docs/common-imu-batchsampling.html> |
| NOTCH | "Harmonic notch filters": <https://ardupilot.org/copter/docs/common-imu-notch-filtering.html> |
| Contract | `v2/docs/DRONE_CONTRACTS_D0.md` (clause or decision ID) |
| Brief | `v2/docs/PHASE_E1_BRIEF.md` |

### 3.1 Frame and motor outputs

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `FRAME_CLASS` | 1 | Quad. Default | Contract V-F1; PARAM |
| `FRAME_TYPE` | 1 | X. Motor order per ArduPilot X. CHECK 9 | Contract [S12], V-F6; MOTORS |
| `SERVO1_FUNCTION` | 33 | Output 1 = motor 1 (front right, counter-clockwise). Default | MOTORS; PARAM |
| `SERVO2_FUNCTION` | 34 | Output 2 = motor 2 (rear left, counter-clockwise). Default | MOTORS; PARAM |
| `SERVO3_FUNCTION` | 35 | Output 3 = motor 3 (front left, clockwise). Default | MOTORS; PARAM |
| `SERVO4_FUNCTION` | 36 | Output 4 = motor 4 (rear right, clockwise). Default | MOTORS; PARAM |

### 3.2 Serial ports

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `SERIAL1_PROTOCOL` | 2 | Companion link is MAVLink2 on UART7. Default | Brief §2; Contract [F1]; BOARD |
| `SERIAL1_BAUD` | 921 | 921600 baud | Brief §2 |
| `SERIAL1_OPTIONS` | 0 | No inversion, no swap. Default | PARAM |
| `BRD_SER1_RTSCTS` | 0 | No flow control: 3-wire link. The default (2) detects flow control at startup; 0 removes that step. CHECK 4 | PARAM; BOARD |
| `SERIAL3_PROTOCOL` | 5 | GPS on USART2, the board's GPS1 port. Default. CHECK 3 | BOARD; HWDEF |
| `SERIAL3_BAUD` | 230 | 230400 baud. Default. The GPS driver finds the baud rate automatically | PARAM |
| `BRD_ALT_CONFIG` | 1 | Makes pad RX6 a UART input. CRSF needs a full UART. CHECK 2 | BOARD; HWDEF |
| `SERIAL7_PROTOCOL` | 23 | RC input (CRSF) on USART6. CHECK 2 | BOARD; CRSF |
| `SERIAL7_OPTIONS` | 0 | CRSF on TX6 and RX6 needs no option. Default | BOARD |

The stream rates of the companion port (`MAV2_*`) are not set. The
companion asks for each message with `MAV_CMD_SET_MESSAGE_INTERVAL`
(contract [F2]).

### 3.3 Radio: ExpressLRS (CRSF), channels and switches

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `RC_PROTOCOLS` | 512 | CRSF only (bit 9). The FC cannot detect a different protocol by mistake | PARAM |
| `RC_OPTIONS` | 8736 | Bit 5 (32): throttle must be at zero to arm (default). Bit 9 (512): no CRSF mode/rate message for ExpressLRS. Bit 13 (8192): 420 kbaud for ExpressLRS | CRSF; PARAM |
| `RSSI_TYPE` | 3 | Signal strength comes from the receiver protocol | CRSF |
| `RCMAP_ROLL` | 1 | Channel order AETR. Default. CHECK 5 | PARAM |
| `RCMAP_PITCH` | 2 | As above | PARAM |
| `RCMAP_THROTTLE` | 3 | As above | PARAM |
| `RCMAP_YAW` | 4 | As above | PARAM |
| `FLTMODE_CH` | 6 | Mode switch on channel 6. ExpressLRS sends channel 5 as 2 positions only | ELRS; Brief 3.11 |
| `FLTMODE1` | 2 | ALT_HOLD. Switch low, 1000 us (slot 1: 1230 us and below) | Contract E1-D16; MODES |
| `FLTMODE2` | 2 | ALT_HOLD (slot 2: 1231 to 1360 us) | Contract E1-D16 |
| `FLTMODE3` | 2 | ALT_HOLD (slot 3: 1361 to 1490 us) | Contract E1-D16 |
| `FLTMODE4` | 4 | GUIDED. Switch centre, 1500 us (slot 4: 1491 to 1620 us). GUIDED is on this slot only, because the move into GUIDED is the launch gate | Contract E1-D4, T03 |
| `FLTMODE5` | 6 | RTL (slot 5: 1621 to 1749 us). A value between centre and high must not be GUIDED | Contract V-D13 |
| `FLTMODE6` | 6 | RTL. Switch high, 2000 us (slot 6: 1750 us and above). The hard abort | Contract [M11], V-D13 |
| `RC5_OPTION` | 0 | Channel 5 has no FC function. It is the ExpressLRS arm channel. Default | ELRS |
| `RC6_OPTION` | 0 | The mode channel must have no other function. Default | AUX |
| `RC7_OPTION` | 31 | Motor Emergency Stop on the guarded momentary switch. CHECK 11 | Brief 3.11; AUX |
| `RC8_OPTION` | 0 | Approve channel: a plain channel. The companion reads it from `RC_CHANNELS`. Default | Contract [F7], §9 |
| `ARMING_RUDDER` | 2 | The pilot arms and disarms with the yaw stick. There is no arm switch. Default | PARAM |

Notes:

- STABILIZE is the fallback for the low position (E1-D16). To use it, set
  `FLTMODE1`, `FLTMODE2` and `FLTMODE3` to 0.
- The FC refuses GUIDED and RTL when it has no position. On the bench
  without a GPS fix, the mode stays the same when you move the switch to
  centre or high.
- `GUID_OPTIONS` 0 means that the transmitter cannot arm in GUIDED. Only
  the companion can (contract [M12]), and its setpoint gate is locked on the
  real FC (B-D1).
- Emergency stop: the motors stop while channel 7 is high. If the pilot
  releases the switch while the FC is armed, the motors can start again.
  ArduPilot disarms after a delay while the stop is active (AUX names
  `DISARM_DELAY`). Hold the switch until the FC is disarmed. Do a test of
  this on the bench with the propellers off.

### 3.4 ESC and motors

The file assumes DShot300 and no bidirectional DShot. AM32 detects the
DShot rate automatically. DShot needs no ESC calibration.

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `MOT_PWM_TYPE` | 5 | DShot300. ArduPilot prefers DShot300 with bidirectional DShot, which is the later step for the notch filter. DShot600 (6) is the alternative that ArduPilot recommends for most vehicles | DSHOT |
| `MOT_PWM_MIN` | 1000 | Default range. DShot does not use ESC end points | TUNE; DSHOT |
| `MOT_PWM_MAX` | 2000 | As above | TUNE; DSHOT |
| `MOT_SPIN_ARM` | 0.10 | Motor speed when armed and on the ground. Default. CHECK 10 | MOTRANGE |
| `MOT_SPIN_MIN` | 0.15 | Lowest motor output in flight. Default. CHECK 10 | MOTRANGE |
| `MOT_SPIN_MAX` | 0.95 | Default | TUNE |
| `MOT_THST_EXPO` | 0.49 | Thrust curve for a 5.1 in propeller: `0.15686 * ln(5.1) + 0.23693`. TUNE gives 0.55 for 5 in. The thrust is not measured (V-F6) | MPCALC; TUNE |
| `MOT_THST_HOVER` | 0.2 | Start value. The FC learns the true value in the first hover | Contract V-F6; MPCALC |
| `MOT_HOVER_LEARN` | 2 | Learn the hover throttle and save it. Default | PARAM; Contract V-F6 |
| `MOT_BAT_VOLT_MAX` | 25.2 | 6 cells at 4.2 V | TUNE |
| `MOT_BAT_VOLT_MIN` | 19.8 | 6 cells at 3.3 V | TUNE |

If bidirectional DShot is necessary later (RPM for the notch filter):

1. Flash `MatekH743-bdshot`. Read the warning on BOARD first: this firmware
   changes the RX6 pin and the buzzer. The buzzer then makes simple beeps
   only, not tunes. That is a problem for the contract [F8] state tones.
2. Set `SERVO_DSHOT_ESC` 1 (AM32), `SERVO_BLH_BDMASK` 15 (outputs 1 to 4)
   and `SERVO_BLH_POLES` 14 (count the magnets of the V2207 to make sure).
3. Enable bidirectional DShot in the AM32 configuration tool.

These three parameters are not in the file. The `MatekH743` firmware has no
bidirectional DShot outputs (BOARD), so they have no use there.

### 3.5 Battery monitor and battery failsafe

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `BATT_MONITOR` | 4 | Analog voltage and current. Board default. CHECK 8 | BOARD; HWDEF |
| `BATT_VOLT_PIN` | 10 | Board default | BOARD; HWDEF |
| `BATT_CURR_PIN` | 11 | Board default | BOARD; HWDEF |
| `BATT_VOLT_MULT` | 11.0 | Firmware default for this board. BOARD lists 10.5. CHECK 7 | HWDEF; BOARD |
| `BATT_AMP_PERVLT` | 40.0 | Board default. Not a value for this ESC. CHECK 8 | HWDEF; BOARD |
| `BATT_CAPACITY` | 1300 | 1300 mAh pack | Contract V-F6 |
| `BATT_ARM_VOLT` | 22.1 | No arming below 22.1 V: `(6 - 1) * 0.1 + 3.6 * 6` | MPCALC |
| `BATT_LOW_VOLT` | 21.6 | Low failsafe at 3.6 V for each cell | MPCALC; FS-BATT |
| `BATT_CRT_VOLT` | 21.0 | Critical failsafe at 3.5 V for each cell | MPCALC; FS-BATT |
| `BATT_LOW_MAH` | 260 | Low failsafe at 20 % left. The mission returns earlier, at 30 % (contract §9 `battery_floor_pct`) | FS-BATT; Contract T17 |
| `BATT_CRT_MAH` | 130 | Critical failsafe at 10 % left | FS-BATT |
| `BATT_LOW_TIMER` | 10 | The voltage must stay low for 10 s. Default | FS-BATT |
| `BATT_FS_VOLTSRC` | 0 | Raw voltage. Default | PARAM |
| `BATT_FS_LOW_ACT` | 2 | Low: RTL | MPCALC; Contract V-D13 |
| `BATT_FS_CRT_ACT` | 1 | Critical: Land | MPCALC |

### 3.6 Radio, ground station and EKF failsafes

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `FS_THR_ENABLE` | 1 | Radio loss: RTL always. Default, and the value in the SITL tests. With no position the FC lands | Contract V-D13, T20; FS-RC |
| `FS_THR_VALUE` | 975 | Default. CRSF reports a lost link itself. The calibrated throttle minimum must stay above this value | FS-RC |
| `RC_FS_TIMEOUT` | 1.0 | Radio failsafe 1 s after the last RC frame. Default | PARAM |
| `FS_OPTIONS` | 0 | No failsafe continues in GUIDED or in a pilot mode. SITL shows 16 as the default, which changes only the ground station failsafe | PARAM; Contract T20 |
| `FS_GCS_ENABLE` | 0 | Off. Default. No ground station flies with the vehicle. Section 9, point 4 | FS-GCS; Contract E1-F6 |
| `FS_EKF_ACTION` | 1 | Bad position estimate: land, in modes that need position. Default | FS-EKF |
| `FS_EKF_THRESH` | 0.8 | Default | FS-EKF |
| `FS_CRASH_CHECK` | 1 | Disarm after a crash. Default. A tether string can stop a propeller (V-D14) | CRASH; Contract V-D14 |

An FC failsafe changes the mode. The mission sees a mode that is not GUIDED
and goes to `ABORT` (T20). The companion does not change the mode back
([M10]).

### 3.7 Fence

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `FENCE_ENABLE` | 1 | Fence on. With the fence on, the FC needs a GPS position to arm | FENCE |
| `FENCE_TYPE` | 3 | Maximum altitude and circle around home (bits 0 and 1). No polygon | FENCE; PARAM |
| `FENCE_ACTION` | 1 | RTL, or Land if RTL is not possible. Default | FENCE; Contract V-D13 |
| `FENCE_RADIUS` | 80 | The mission returns at 60 m (`geofence_radius_m`). The FC fence is the second barrier, 20 m farther | Contract §9, T17 |
| `FENCE_ALT_MAX` | 50 | 50 m above home. Room for RTL at 15 m and for a balloon above the 10 m search altitude | Contract §9 (`search_alt`), E2-D6 |
| `FENCE_MARGIN` | 2 | Default | PARAM |

### 3.8 RTL and LAND

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `RTL_ALT_M` | 15 | Return at 15 m or higher. Default. Make sure that this is above the trees at the site | RTL |
| `RTL_CLIMB_MIN_M` | 0 | Default | RTL |
| `RTL_SPEED_MS` | 5 | Return at 5 m/s. From the fence at 80 m this is 16 s | RTL; PARAM |
| `RTL_LOIT_TIME` | 5000 | Wait 5 s above home before the descent. Default | RTL |
| `RTL_ALT_FINAL_M` | 0 | Land at the end of RTL. Default | RTL |
| `LAND_SPD_MS` | 0.5 | Last part of the descent at 0.5 m/s. Default. The vehicle is top-heavy, so do not increase it | LAND |

### 3.9 GUIDED mode

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `GUID_TIMEOUT` | 3 | Default, and the value in the SITL tests. With no new setpoint for 3 s the vehicle stops | Contract E1-F21, E2-F1; GUIDED |
| `GUID_OPTIONS` | 0 | Default. The transmitter cannot arm in GUIDED. The pilot's yaw stick is not ignored | Contract E1-D4, [M12]; PARAM |

### 3.10 Arming checks

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `ARMING_SKIPCHK` | 0 | Skip no check. Default | PREARM |

### 3.11 Compass and EKF sources

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `COMPASS_ENABLE` | 1 | Default | COMPASS |
| `COMPASS_USE` | 1 | Use the compass for yaw. Default | COMPASS |
| `COMPASS_EXTERNAL` | 1 | The compass is on a cable. The board has no internal compass | HWDEF; PARAM |
| `COMPASS_ORIENT` | 0 | Placeholder. CHECK 6 | COMPASS |
| `COMPASS_AUTO_ROT` | 2 | The calibration finds the orientation and writes it. Board default | HWDEF; PARAM |
| `COMPASS_AUTODEC` | 1 | Declination from the GPS position. Default | PARAM |
| `COMPASS_LEARN` | 0 | No automatic offset learning. Default | PARAM |
| `EK3_SRC1_POSXY` | 3 | GPS. Default | EKFSRC |
| `EK3_SRC1_VELXY` | 3 | GPS. Default | EKFSRC |
| `EK3_SRC1_POSZ` | 1 | Barometer. Default | EKFSRC |
| `EK3_SRC1_VELZ` | 3 | GPS. Default | EKFSRC |
| `EK3_SRC1_YAW` | 1 | Compass. Default | EKFSRC |
| `EK3_GSF_RUN_MASK` | 3 | Both EKF cores run the GSF yaw estimator (GPS and IMU, no compass). Default | PARAM; NOCOMPASS |
| `EK3_GSF_USE_MASK` | 3 | Both EKF cores can reset yaw from the GSF estimate when the compass yaw is bad. This is the fallback. Default | PARAM; NOCOMPASS |

If the compass is bad on this airframe (it is near the battery leads), the
alternative is flight with no compass: `EK3_SRC1_YAW` 8 and `COMPASS_USE` 0
(NOCOMPASS). That is a decision for the owner after the first logs.

### 3.12 GPS

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `GPS1_TYPE` | 2 | u-blox. The default (1, automatic) also works | PARAM |
| `GPS1_GNSS_MODE` | 0 | Keep the constellations that the NEO-M9N has configured. Default | PARAM |
| `GPS_AUTO_CONFIG` | 1 | ArduPilot configures the receiver (rate, messages). Default | PARAM |

### 3.13 Start tune

These values are the start values that ArduPilot recommends for the
propeller size. They are not a tuning result. The PID gains stay at the
firmware defaults.

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `INS_GYRO_FILTER` | 74 | `289.22 * 5.1^-0.838` = 73.8 Hz. TUNE gives 80 Hz for 5 in | MPCALC; TUNE |
| `INS_ACCEL_FILTER` | 10 | 10 Hz | TUNE; MPCALC |
| `ATC_RAT_RLL_FLTD` | 37 | `INS_GYRO_FILTER` / 2 | TUNE; MPCALC |
| `ATC_RAT_RLL_FLTT` | 37 | As above | TUNE; MPCALC |
| `ATC_RAT_PIT_FLTD` | 37 | As above | TUNE; MPCALC |
| `ATC_RAT_PIT_FLTT` | 37 | As above | TUNE; MPCALC |
| `ATC_RAT_YAW_FLTT` | 37 | As above | TUNE; MPCALC |
| `ATC_ACC_R_MAX` | 1674 | deg/s². MPCALC polynomial for 5.1 in (167 400 centideg/s²). TUNE has no value for 5 in. The default is 1100 | MPCALC |
| `ATC_ACC_P_MAX` | 1674 | As above | MPCALC |
| `ATC_ACC_Y_MAX` | 314 | deg/s². `-900 * 5.1 + 36000` = 31 400 centideg/s². The default is 270 | MPCALC |
| `ATC_THR_MIX_MAN` | 0.1 | Start value. Default. MPCALC says: set 0.5 after the first flight | MPCALC |
| `ATC_ANGLE_MAX` | 30 | 30 degrees maximum lean. Default. V-D7 lets the owner change it later | Contract V-D7; PARAM |
| `PSC_D_ACC_P` | 0.02 | 0.1 times `MOT_THST_HOVER` (0.2). The default (0.05) is for a hover throttle of 0.5. Set it again from the learned hover throttle | MPCALC |
| `PSC_D_ACC_I` | 0.04 | 0.2 times `MOT_THST_HOVER` (0.2). The default is 0.1. Set it again from the learned hover throttle | MPCALC |

### 3.14 Speed limits for the first flights

The owner increases these when the vehicle is tuned.

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `PILOT_SPD_UP` | 1.5 | Pilot climb limit in ALT_HOLD, m/s. The default is 2.5 | PARAM |
| `PILOT_SPD_DN` | 1.0 | Pilot descent limit, m/s. The default (0) uses `PILOT_SPD_UP` | PARAM |
| `LOIT_SPEED_MS` | 5 | Position-hold speed limit, m/s. The default is 12.5 | PARAM |
| `WP_SPD` | 5 | Horizontal speed of the waypoint controller, m/s (position targets; RTL when `RTL_SPEED_MS` is 0). The default is 10 | PARAM |
| `WP_SPD_UP` | 1.5 | Climb speed of the waypoint controller, m/s. The default is 2.5 | PARAM |
| `WP_SPD_DN` | 1.0 | Descent speed of the waypoint controller, m/s, and the first part of a landing (`LAND_SPD_HIGH_MS` is 0). The default is 1.5 | PARAM; LAND |
| `WP_ACC` | 2.5 | Horizontal acceleration of the waypoint controller, m/s². Default | PARAM |

**These do not limit GUIDED velocity setpoints.** In the SITL flight of
section 10, an 8 m/s setpoint gave 8.1 m/s with `WP_SPD` 5, and 2 m/s
vertical setpoints gave 2.1 m/s with `WP_SPD_UP` 1.5 and `WP_SPD_DN` 1.0.
The companion's own limits (contract [F6]: 5.0 m/s horizontal, 2.0 m/s
vertical) are the only limit on a commanded velocity. `ATC_ANGLE_MAX` limits
the lean angle.

### 3.15 Logging

| Parameter | Value | Why | Source |
| --- | --- | --- | --- |
| `LOG_BACKEND_TYPE` | 1 | Log to the SD card. Default | PARAM |
| `LOG_BITMASK` | 180223 | The default (180222) plus bit 0, fast attitude. The default has PID logging (bit 12) | PARAM |
| `LOG_DISARMED` | 0 | Log only when armed. Default | PARAM |
| `INS_LOG_BAT_MASK` | 1 | Batch-sample the first IMU, for the vibration spectrum | BATCH |
| `INS_LOG_BAT_OPT` | 4 | Sample before and after the filters | BATCH |
| `INS_HNTCH_ENABLE` | 0 | The harmonic notch stays off. Section 8 | NOTCH |

## 4. Not set here on purpose

The file does not hold these. Each one is a result of work on the real
vehicle, or it belongs to a different owner.

- **Accelerometer and level calibration** (`INS_ACC*OFFS`, `INS_ACC*SCAL`,
  `AHRS_TRIM_*`).
- **Compass calibration** (`COMPASS_OFS*`, `COMPASS_DIA*`, `COMPASS_ODI*`,
  `COMPASS_DEV_ID*`, `COMPASS_MOT*`) and the declination.
- **Radio calibration** (`RCn_MIN`, `RCn_MAX`, `RCn_TRIM`, `RCn_REVERSED`).
- **PID tuning results** (`ATC_RAT_*_P`, `_I`, `_D`, `ATC_ANG_*_P`). They
  stay at the firmware defaults until a manual tune or AutoTune.
- **The harmonic notch filter** (`INS_HNTCH_*`, except `ENABLE` 0).
- **The learned hover throttle.** `MOT_THST_HOVER` 0.2 is a start value.
- **Bidirectional DShot and ESC telemetry** (`SERVO_DSHOT_ESC`,
  `SERVO_BLH_*`). Section 3.4.
- **MAVLink stream rates** (`MAV2_*`). The companion asks for its messages.
- **`ATC_RAT_YAW_FLTE`.** TUNE gives 2 Hz. The firmware default is 2.5 Hz.
  The parameter metadata gives a range of 5 to 50. The file does not set a
  value that is out of the documented range, so the default stays.
- **`PSC_JERK_NE`.** It stays at the default (5 m/s³). E2-F1 names it as a
  source of lag for acceleration setpoints. That is E2b work.
- **Motor direction.** The AM32 configuration tool holds it.
- **Simulator values** (`SIM_*`) and site values.

`check_params.py lint` fails if one of the calibration, tuning or simulator
names is in the file.

## 5. Differences from the SITL defaults

The software was tested against the pinned SITL (ArduCopter 4.7.0,
`copter.parm`, the harness defaults). The baseline agrees with it on the
values the mission depends on: `FRAME_CLASS` 1, `FRAME_TYPE` 1 (cone
frame), `FS_THR_ENABLE` 1, `GUID_TIMEOUT` 3, `GUID_OPTIONS` 0,
`ATC_ANGLE_MAX` 30, `WP_ACC` 2.5, `RTL_ALT_M` 15, and the 6S limits
`MOT_BAT_VOLT_MAX` 25.2 and `MOT_BAT_VOLT_MIN` 19.8.

It differs on purpose here:

| Item | SITL | Baseline | Why |
| --- | --- | --- | --- |
| Mode channel | 5 | 6 | ExpressLRS sends channel 5 as 2 positions. The mission reads the mode from `HEARTBEAT`, not the channel |
| Low switch position | STABILIZE (harness) | ALT_HOLD | E1-D16 says so for both |
| `FLTMODE5` (1621 to 1749 us) | GUIDED (harness) | RTL | Only the centre slot is GUIDED |
| `WP_SPD` | 10 m/s | 5 m/s | First-flight limit for position targets and RTL. It does not limit velocity setpoints (section 3.14) |
| `WP_SPD_UP`, `WP_SPD_DN` | 2.5, 1.5 m/s | 1.5, 1.0 m/s | First-flight limit for RTL and landing. The effect on the time of a GUIDED takeoff to 10 m was not measured |
| Fence | Off (radius 150 m) | On, 80 m, 50 m high | The FC fence is not in the SITL tests |
| Battery failsafes | Off | On | Not in the SITL tests |
| `FS_OPTIONS` | 16 | 0 | Changes the ground station failsafe only, which is off |
| `MOT_THST_EXPO` | 0.65 | 0.49 | The SITL frame file models 0.65. The real thrust curve is not measured |
| `MOT_THST_HOVER` | 0.21 (learned in SITL) | 0.2 | Start value; the FC learns it |
| `PSC_D_ACC_P`, `PSC_D_ACC_I` | 0.05, 0.1 | 0.02, 0.04 | Low hover throttle |
| Filters, `ATC_ACC_*` | defaults (20 Hz, 1100) | 74 Hz, 37 Hz, 1674 | 5.1 in start tune |
| `MAV2_EXTRA1` | 10 | not set | SITL needs it for the [F1] (b) proof only |

Before the first autonomous flight, do one of these: run the SITL gate with
this file as a defaults file, or set the limits back to the tested values.

A SITL flight with the flight part of the baseline was done on 2026-11-27.
The numbers are in section 10.

## 6. How to load the file and compare

Use Mission Planner. The file is in the Mission Planner `NAME,VALUE` format.
MAVProxy also reads it (`param load`). QGroundControl: the current source
(branch `master`, read on 2026-11-27) reads this 2-column format and shows a
comparison before it writes. Older QGroundControl releases read only their
own 5-column format and skip every line of this file. Neither was tried
here. With QGroundControl, continue only if the comparison shows the
parameters of this file.

The file holds 127 of about 1300 parameters. A parameter that is not in the
file stays as it is on the FC. Thus start from a clean FC.

1. Connect the FC with USB. No battery.
2. Save the FC's parameters first: CONFIG, Full Parameter List, "Save to
   file". Keep this file. It is the way back.
3. On a used FC, reset to the defaults: "Reset to Default", then reboot.
   Do this before the calibrations, because it erases them.
4. Compare: "Compare Params", then select `skyweave_cone_x.param`. Mission
   Planner shows each parameter that differs, with the FC value and the
   file value.
5. Read the list. Each line must be a value from section 3. Tick the lines
   to change, then "Continue".
6. "Write Params". Then reboot the FC.
7. Do steps 4 to 6 again. Some parameters appear only after a reboot (for
   example `BATT_*` after `BATT_MONITOR`, and the `SERIAL7` change after
   `BRD_ALT_CONFIG`). Stop when the comparison shows no difference.
8. If Mission Planner says that a parameter does not exist, stop and find
   the cause. The usual cause is a different firmware version or target.

To check the file without an FC:

```bash
python3 v2/deploy/fc/check_params.py lint
python3 v2/deploy/fc/check_params.py meta --download
```

## 7. Bench sequence

The rule for the bench (contract B-D1): **propellers off, and no power to
the motors**, until step 9. Step 9 is optional, and the owner decides it.

1. **Propellers off.** Remove all four. Keep them off for the full
   sequence.
2. **Flash.** Flash ArduCopter 4.7.x for `MatekH743`
   (<https://firmware.ardupilot.org/Copter/stable-4.7.0/MatekH743/>). Use
   the file `arducopter_with_bl.hex` for the first flash in DFU mode. Use
   `arducopter.apj` for an update. Then do check 1 of section 2.
3. **Load the file.** Do section 6. Power is from USB only.
4. **Do the checks of section 2** that need no battery: 2, 3, 4, 5, 11, 12.
5. **Calibrations.** Accelerometer (6 positions) and level. Then the
   compass calibration, away from steel and from the bench power supply.
   Then read `COMPASS_ORIENT` (check 6). Then the radio calibration.
6. **Receiver and switch check.** On the Radio Calibration screen:
   - All four sticks move the correct bars in the correct direction.
   - Channel 6 shows about 1000, 1500 and 2000 us. The flight mode shows
     ALT_HOLD at low. With a GPS fix it shows GUIDED at centre and RTL at
     high. With no fix the FC refuses those two modes.
   - Channel 7 is low when released and high when held.
   - Channel 8 goes above 1700 us when the approve switch is on
     (`approve_pwm_high`, contract §9).
   - Set the transmitter to off. Mission Planner must show that the RC
     link is lost. Set it to on again.
7. **Battery voltage check.** This step connects the battery, so the ESC
   has power. Make sure again that the propellers are off. Do check 7. If
   the owner does not want power on the ESC now, do this check later.
8. **Companion link check.** Start the companion in its bench mode
   (`real_fc`, contract [F1] (c), B-D1) on UART7 at 921600 baud. The
   companion must show the FC link up and a fresh attitude. Turn the
   vehicle by hand and make sure that the attitude follows. Ask for a tone
   ([F8]): this needs a buzzer on the FC. After each FC reboot, start the
   companion again (section 9, point 3).
9. **Motor test. Only with the propellers off. Only when the owner chooses
   to power the motors.** In Mission Planner: SETUP, Optional Hardware,
   Motor Test. Start with a low throttle percent. Motor A is the front
   right motor. The test then goes clockwise, seen from above: B rear
   right, C rear left, D front left. If a different motor turns, the order
   is wrong: do check 9. Then check each spin direction against the QUAD X
   picture on the MOTORS page. Then find the lowest percent at which all
   four motors turn reliably, and set `MOT_SPIN_ARM` and `MOT_SPIN_MIN`
   (check 10). Then do the emergency stop test (section 3.3).
10. **Record.** Save the FC's parameters to a file again. Record each
    value that changed, so that the baseline can be updated with a recorded
    reason. A value from the bench stays Provisional until a recorded
    result exists.

## 8. Later steps (not bench work)

- **First hover.** ArduPilot's tuning guide does the first flight in
  STABILIZE (section 3.3 gives the fallback setting). After a hover of
  30 s in calm air, read the learned `MOT_THST_HOVER`. Set `PSC_D_ACC_P`
  to 0.1 times it and `PSC_D_ACC_I` to 0.2 times it. Set `ATC_THR_MIX_MAN`
  to 0.5.
- **RTL as the failsafe action.** RTL needs a good compass and GPS. Before
  the first good position-hold or RTL test, the owner can decide to use
  Land for the failsafes (`FS_THR_ENABLE` 3, `BATT_FS_LOW_ACT` 1,
  `FENCE_ACTION` 2). The contract value is RTL (V-D13), so the file
  holds RTL.
- **Harmonic notch.** Do not enable it blind. Fly one flight with the
  batch logging in this file. Look at the spectrum (Mission Planner FFT, or
  the Filter Review tool that NOTCH names). Then set the notch from the
  log. ArduPilot says that ESC RPM (bidirectional DShot, section 3.4) is the
  best frequency source, and the throttle-based notch is the next best.
- **PID tune.** Manual tune or AutoTune, after the notch.
- **Compass interference from motor current.** If the logs show it, do the
  `COMPASS_MOT` calibration or use the no-compass alternative.

## 9. Open points for the owner

1. **Hover throttle.** V-F6 gives thrust-to-weight 8 to 10. Then the hover
   thrust is 0.10 to 0.125 in ArduPilot's units, at or below the
   `MOT_THST_HOVER` minimum (0.125). ALT_HOLD can then be too sensitive.
   The first hover gives the answer (`harness/frames/README.md` has the
   same finding).
2. **Battery percent.** It is wrong until the current scale is calibrated
   (check 8). The mission's battery floor (T17) uses it.
3. **FC reboot and the companion.** The companion's message requests are
   not saved in the FC. After an FC reboot the FC sends only its heartbeat
   until the companion asks again. On the bench, start the companion again
   after each FC reboot. An alternative is to set the `MAV2_*` stream rates
   in this file.
4. **Companion loss in GUIDED.** If the companion stops, the vehicle stops
   after `GUID_TIMEOUT` and hovers in GUIDED. The pilot's switch and the
   battery failsafe are the exits (E1-F6). The ground station failsafe
   (`FS_GCS_ENABLE`) with the companion's heartbeat is a possible FC-side
   exit. It is off here. Decide it before autonomy.
5. **`GUID_TIMEOUT`.** 3 s is the tested value. At 2.5 m/s the vehicle
   moves 7.5 m in that time (E1-F21). A shorter value is possible after a
   SITL run.
6. **No FC speed limit on velocity setpoints.** Section 3.14. The FC flies
   the velocity that the companion sends. Only [F6] limits it.

## 10. Validation

`check_params.py` has three checks. None of them talks to an FC.

| Check | What it proves | Needs |
| --- | --- | --- |
| `lint` | The file parses, has no duplicate, holds the 18 project rules (each names its contract clause), and has no calibration, tuning or simulator name | Python 3.10, nothing else |
| `meta` | Each name is in ArduPilot's parameter metadata for Copter stable-4.7.0 (`apm.pdef.xml`), and each value is in the documented range, is a documented value, or sets only documented bits | The metadata file in `<repo>/_local/ardupilot-pdef/` (git-excluded); `--download` gets it from `autotest.ardupilot.org` |
| `sitl` | The pinned SITL starts with the file as a defaults file, and each parameter reads back with the file's value | Linux x86-64: `v2/docker/sitl/run.sh shell -c "python deploy/fc/check_params.py sitl"` |

The script is not in the pytest tiers. `meta --download` needs the network
and `sitl` needs the SITL platform.

Results on 2026-11-27 (git revision of this file's commit):

- `lint`: 127 parameters, 18 rules, 0 failed.
- `meta`, stable-4.7.0 (file SHA-256 `d0b75b4e…d1d4107`): 127 names found,
  0 failed. 53 values checked against a range, 51 against a value list, 15
  against a bit list. 8 have no documented range (name check only):
  `BATT_AMP_PERVLT`, `BATT_ARM_VOLT`, `BATT_CAPACITY`, `BATT_CRT_MAH`,
  `BATT_CRT_VOLT`, `BATT_LOW_MAH`, `BATT_LOW_VOLT`, `BATT_VOLT_MULT`.
  The same check against stable-4.7.1: 0 failed.
- `sitl`, pinned ArduCopter 4.7.0: 125 parameters read back equal, 0
  differ. 2 are not in SITL and were checked by the metadata only:
  `BRD_ALT_CONFIG`, `BRD_SER1_RTSCTS`.

What these checks do not prove: that a value is right for this vehicle.
SITL accepts a value that is out of range, and the metadata file lists some
names that a given firmware build does not have. The real FC is the last
check (section 6, step 8).

### SITL flight with the baseline (simulated, Provisional)

One flight for each column, on 2026-11-27: pinned SITL 4.7.0, the cone
frame `cone_x.json`, calm air, speedup 2, commands from the ground station
link only. The right column adds the 71 flight parameters of the baseline
as defaults. The radio, serial, battery sensor, compass, GPS, DShot and
logging names were left out, because they change how SITL itself runs. This
is not evidence for the real vehicle. The script that flew it is a scratch
file and is not in the repo, so these numbers are a one-time observation.

| Quantity | SITL defaults, cone frame | With the baseline |
| --- | --- | --- |
| Hover at 10 m, 20 s: altitude standard deviation | 0.001 m | 0.004 m |
| Hover throttle (`VFR_HUD`) | 20.0 % | 23.9 % |
| Roll, pitch standard deviation in hover | 0.02, 0.04 deg | 0.02, 0.04 deg |
| Peak speed for a 2 m/s north setpoint | 1.99 m/s | 1.99 m/s |
| Peak speed for an 8 m/s setpoint | 8.08 m/s | 8.12 m/s |
| Speed 8 s after the setpoints stop (`GUID_TIMEOUT` 3) | 0.07 m/s | 0.06 m/s |
| Peak climb, peak sink for 2 m/s setpoints | 2.04, 1.93 m/s | 2.13, 2.13 m/s |
| 5 m/s run to the north | No fence: the script stopped it at 120 m, in GUIDED | "Circle fence breached" at 80.0 m; the FC went to RTL, came back at 5.0 m/s, landed and disarmed |

What it shows:

- The start tune, the lower `PSC_D_ACC` gains and the first-flight limits
  fly in SITL. The hover throttle reads higher because `MOT_THST_EXPO` 0.49
  does not agree with the frame file's 0.65.
- The fence at 80 m changes the mode to RTL. The mission then sees a mode
  that is not GUIDED (T20).
- `WP_SPD`, `WP_SPD_UP` and `WP_SPD_DN` do not limit velocity setpoints.
- Before a position estimate, SITL reported "PreArm: Fence enabled, need
  position estimate".

Two earlier tries of the baseline flight got no telemetry after one stream
request, when the flight was the second SITL instance of one process. A
repeated request fixed it. The cause was not isolated. It is a property of
the scratch script, not a known property of the baseline.
