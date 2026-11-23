# Source notes: controls and state estimation behind the SkyWeave drone stack

Audience: one learner. Math background: Calculus 1, 2 and 3 (derivatives,
integrals, chain rule, partial derivatives, gradients, tangent planes,
vectors, spherical coordinates). No formal linear algebra and no formal
differential equations course. Explain every matrix idea and every ODE idea
from zero, with pictures and with the drone as the reason for it.

Goal of the video: after it, the learner can read the SkyWeave drone code
(tracker.py, guidance.py, camera.py, vehicle_state.py) and the E2 brief and
know what each equation does, why it makes sense physically, and what the
numbers mean. Not rigorous proofs. Intuition first, then the equation, then
a worked number from the real code.

Style rules for the script:
- Simple, short sentences. Define each new word the first time.
- No em dashes in on-screen text or in spoken text.
- Every major idea gets one guided practice problem: the host poses it,
  says "pause and try", the learner tries out loud (sometimes with a small,
  common mistake that the host corrects), then the host solves it step by
  step on screen. All answers below are checked.
- Tie every concept to the place it lives in the code (file and function
  name) and to the physical drone.
- Label honesty: PN (proportional navigation) and the world-angle tracker
  are PLANNED (phase E2, parts E2a/E2b). Today's code: per-axis Kalman
  filters in pixels, pure pursuit, an open-loop terminal segment. Say
  "planned" for the planned parts. Every number in the code is Provisional
  (a starting guess to be tuned in simulation), not measured.

## 0. The system in one picture (scene 1)

The vehicle: a small X quadcopter ("cone airframe") with a tall tower. The
camera sits at the nose tip and, at hover, looks straight UP along the
thrust axis (mount name `cone_up`), with a 100 degree lens. The job (phase
E2): fly below a balloon, find it, climb into it, and touch it nose first.

The loop, in order (this is a feedback loop; it runs over and over):
1. Camera takes a frame (60 frames per second in the sim). Each frame has a
   capture time `t_cap` in board milliseconds.
2. percepd (the detector, on the RV1106 board) finds boxes: center (u, v),
   width w, height h, in pixels. Sent as a "detection packet".
3. Tracker (`drone/tracker.py`) smooths the boxes with Kalman filters, gives
   each object a stable track id, estimates how fast each box moves and
   grows. Output: "track packets".
4. Mission state machine (`drone/mission.py`) decides what phase we are in:
   SEARCH, ACQUIRING, ENGAGED, COASTING, TOUCH, LOST, RETURN, LAND, ...
5. Guidance (`drone/guidance.py`) turns the engaged track into a velocity
   command (vn, ve, vd, yaw_rate) 10 times per second (planned: 50 per
   second during the final pass).
6. fc_link (`drone/fc_link.py`) sends the command to the flight controller
   over MAVLink.
7. The flight controller (ArduCopter 4.7.0) runs its own fast inner loops:
   velocity, acceleration, tilt angle, body rate, motor speeds.
8. The motors move the vehicle. The camera sees a new picture. Back to 1.

Also flowing back: the flight controller sends its attitude estimate (roll,
pitch, yaw) about 50 times per second. That estimate comes from the flight
controller's own extended Kalman filter (EKF3), which fuses the gyroscope,
accelerometer, compass, barometer and GPS.

Two big halves:
- State estimation: "what is true right now?" (tracker, attitude, range).
- Control and guidance: "what should I do about it?" (pursuit, PN, PID).

Open loop vs closed loop: open loop = do a fixed plan without looking (like
today's terminal segment [G5]: at commit, fly straight along the line of
sight for a fixed time, no camera input). Closed loop = measure, compare
with the goal, correct, repeat (pursuit, the planned PN pass).

## 1. State and differential equations (scene 2)

State: the smallest set of numbers that, together with the inputs, lets you
predict the future. For a point moving on a line: position x and velocity v.
State vector s = [x, v].

A differential equation is a rule for how the state changes:
  dx/dt = v,   dv/dt = a   (a = acceleration, the input).
It does not tell you where you are. It tells you which way you are moving
right now. Picture: a vector field of little arrows in the (x, v) plane;
the state flows along the arrows.

Computers work in steps of size dt (Euler step, a first-order Taylor
approximation from Calc 1):
  x_next = x + v * dt
  v_next = v + a * dt

Constant velocity model (the tracker's model, [K1]): assume a = 0 on
average. Then in matrix form:
  [x_next]   [1  dt] [x]
  [v_next] = [0   1] [v]
  F = [[1, dt], [0, 1]]
Explain matrix-vector multiplication as "each output row is a weighted sum
of the inputs": row 1 says new position = 1*x + dt*v; row 2 says new
velocity = 0*x + 1*v. Visual: F shears the (x, v) plane (points with high
velocity slide farther right).

In the code this is `_Axis.predict`: `self.x += self.r * dt`.
In the tracker each box has FOUR such filters, one per axis: center u,
center v, width w, height h. State per axis: [pos, rate].

Practice problem A: a box center is at u = 500 px moving at 120 px/s. One
frame later at 60 fps (dt = 1/60 s), where does the model predict it?
Answer: 500 + 120/60 = 502 px; rate stays 120 px/s.

## 2. Proportional feedback and exponential decay (scene 3)

Error e = where you want to be minus where you are. Proportional control:
command a velocity proportional to the error: v = Kp * e.
Since velocity is the rate of change of position, the error obeys
  de/dt = -Kp * e.
This is the most important differential equation in control. Solution
(check by differentiating): e(t) = e0 * exp(-Kp * t). The error shrinks by
the same FRACTION every second. Time constant T = 1/Kp: after T the error
is 37% of the start (63% done); after 3T about 5% is left.

In the code (`PurePursuit.command`): v = Kp * q with Kp = 0.8 per second,
where q is the vector from the vehicle to the "standoff point" near the
target. T = 1/0.8 = 1.25 s. The same pattern appears three times:
- Pursuit: v = Kp * q, Kp = 0.8 /s.
- Yaw law: yaw_rate = K_yaw * az, K_yaw = 1.5 /s (az = bearing of target
  minus heading). T = 0.67 s.
- Altitude hold: v_D = K_alt * (rel_alt - search_alt), K_alt = 0.5 /s.

Real-world guards added on top (all in `PurePursuit.command`):
- Deadband: if |q| <= 0.25 m command zero (do not chase noise).
- Saturation (clamp): |v| <= v_max (trial speed cap). Yaw rate <= 90 deg/s.
- Rate limit: |v_k - v_(k-1)| <= a_max * dt, a_max = 2 m/s^2 (no sudden
  jumps in command; acts like a gentle acceleration limit).

Practice problem B: the standoff point is 5 m ahead. Kp = 0.8.
(1) What speed is commanded? 0.8 * 5 = 4 m/s.
(2) The trial cap is v_max = 3 m/s. Command? Clamped to 3 m/s.
(3) The vehicle starts at rest. Setpoints are 10 per second (dt = 0.1 s)
    and a_max = 2 m/s^2. Max change per step? 2 * 0.1 = 0.2 m/s. Steps to
    reach 3 m/s? 15 steps = 1.5 s.
(4) Without clamps, how long until the error is 5% of the start?
    3T = 3 * 1.25 = 3.75 s.

Practice problem B2 (yaw): the target is 30 deg (0.524 rad) right of the
nose. K_yaw = 1.5 /s. yaw_rate = 0.785 rad/s = 45 deg/s. Below the 90 deg/s
clamp, so it is sent as is.

Discrete-time warning (sets up scene 13): in steps, e_next = (1 - Kp*dt)*e.
Kp*dt = 0.08 gives factor 0.92 per step: smooth. If Kp*dt were 2.5, the
factor is -1.5: the error flips sign and GROWS every step. Too much gain for
the step size (or with delay) makes a loop unstable.

## 3. Newton, second-order systems, PID, cascaded loops (scene 4)

You cannot command position directly. Motors make thrust (force).
F = m a. Position is the integral of velocity, which is the integral of
acceleration: a "double integrator". Push on a double integrator with a
pure spring-like force (P only) and it oscillates forever:
  x'' = -Kp x  ->  sinusoids.
Add damping (D term, force against velocity):
  x'' + Kd x' + Kp x = 0.
Mass-spring-damper. Natural frequency w_n = sqrt(Kp); damping ratio
zeta = Kd / (2 sqrt(Kp)). zeta < 1 overshoot and ring; zeta = 1 fastest with
no overshoot (critical); zeta > 1 sluggish.

Eigenvalues (introduce gently): try x = exp(lambda t). Then
lambda^2 + Kd lambda + Kp = 0. Roots are the system's natural "modes".
Real part negative = decays. Imaginary part = oscillation frequency.
In matrix form the state [x, v] obeys s' = A s with A = [[0, 1], [-Kp, -Kd]]
and the eigenvalues of A are those same roots. Eigenvalue meaning: a
direction that the system only stretches or shrinks, never turns.

Practice problem C: Kp = 4, Kd = 2. w_n = 2 rad/s, zeta = 2/(2*2) = 0.5.
Roots: lambda = -1 +/- i*sqrt(3). Decays like exp(-t), oscillates at
sqrt(3) = 1.73 rad/s. Overshoot = exp(-zeta*pi/sqrt(1-zeta^2)) =
exp(-1.81) = 0.16, so about 16% overshoot. For critical damping keep
Kp = 4 and set Kd = 4.

PID: P = spring toward the goal (present error), D = damper (rate of the
error, predicts), I = integral of the error (past; removes a steady offset
such as constant wind pushing the vehicle). ArduCopter uses P and PID loops.

Cascade (ArduCopter's structure, the part SkyWeave does NOT write):
position error -> P -> velocity target -> PID -> acceleration target ->
convert to lean angles and thrust -> angle P -> body-rate target -> rate
PID -> motor mixer -> 4 motor commands. Inner loops run much faster (the
main loop runs hundreds of times per second) than outer loops. Rule of
thumb: each outer loop should be several times (about 5 to 10x) slower
than the loop inside it, so the outer loop can treat the inner loop as
"instant". SkyWeave guidance (10 Hz velocity setpoints) sits on top of this
whole stack.

Gravity feedforward (V-D2, planned PN pass): the planned pass sends
ACCELERATION setpoints (MAVLink SET_POSITION_TARGET_LOCAL_NED, type mask
1031 or 1087 to be proven on SITL). ArduCopter adds gravity for us: to
accelerate sideways at a_h while not falling, thrust must point up AND
sideways. Tilt angle = atan(a_h / g). Thrust = m*sqrt(a_h^2 + g^2).
Feedforward = you add the known need (gravity) directly instead of waiting
for an error to build up.

Practice problem C2: a horizontal acceleration of 2 m/s^2 with g = 9.81.
Tilt = atan(2/9.81) = 11.5 deg. Thrust = sqrt(4 + 96.2)/9.81 = 1.02 times
hover thrust. Note: with the cone_up camera, tilting the vehicle tilts the
camera too. That is why de-rotation (scene 6) matters.

## 4. Frames and rotations (scene 5)

Three coordinate frames:
- NED world frame: x North, y East, z Down (aviation convention: Down is
  positive, so "up" has negative z).
- Body frame: x out the front, y out the right side, z out the bottom.
- Camera frame: X right in the image, Y down in the image, Z out of the
  lens (the optical axis).

A rotation matrix converts a vector from one frame to another. Its COLUMNS
are where the source axes land in the target frame. Rotation matrices keep
lengths and angles (orthonormal), their inverse is their transpose, and
their determinant is +1.

Roll (about x), pitch (about y), yaw (about z), each a simple 2D rotation
with cos and sin in a 3x3 matrix (`rot_x`, `rot_y`, `rot_z` in camera.py).
ArduPilot's convention [C3]: R_ned_body = Rz(yaw) * Ry(pitch) * Rx(roll).
Order matters: matrix products do not commute. Show with a book: rotate
x then y vs y then x gives different poses.

Camera mount: `cone_up` R_body_cam = [[1,0,0],[0,-1,0],[0,0,-1]].
Read the columns: camera X (image right) = body x (forward); camera Y
(image down) = body -y (left); camera Z (optical axis) = body -z (up).
So at hover the lens looks straight up and the wide image axis points
fore-aft.

Chain: a direction in the camera frame goes to the world by
  los_ned = R_ned_body(attitude) * R_body_cam * ray_cam.
Read right to left: camera -> body -> world.

## 5. Pinhole camera, de-rotation, timing (scene 6)

Pinhole model (similar triangles): a point at camera coordinates
(X, Y, Z) lands at pixel u = cx + f * X/Z, v = cy + f * Y/Z.
f = focal length in pixels; (cx, cy) = image center = (959.5, 599.5) on the
1920 x 1200 grid. The inverse: a pixel gives a RAY (a direction, not a
point): ray = ((u - cx)/f, (v - cy)/f, 1). Depth is lost: one pixel = a
whole line of possible points.

Lens: 100 deg horizontal field of view. Half of 1920 is 960 px and the half
angle is 50 deg, so f = 960 / tan(50 deg) = 805.5, rounded to 806 px.

Practice problem D:
(1) A box center is 403 px right of center. Angle off the axis?
    atan(403/806) = atan(0.5) = 26.6 deg.
(2) At the frame edge (960 px): atan(960/806) = atan(1.19) = 50.0 deg.
(3) Why a level vehicle sees only targets at least 40 deg above the horizon
    (E2-D6): the camera looks straight up; the edge is 50 deg from
    straight up; 90 - 50 = 40. With a height gap dh under the balloon, the
    balloon is visible within dh / tan(40 deg) = 1.19 * dh horizontally.

De-rotation [G1] (`guidance.derotate`): turn the pixel ray into a world
direction using the attitude at the moment the frame was CAPTURED (t_cap),
not when it arrived. Attitude samples come at about 50 Hz; the code
interpolates roll, pitch and yaw linearly between the two samples around
t_cap (`VehicleState.attitude_at`), never extrapolates, and marks the
packet "attitude-degraded" if the nearest sample is more than 100 ms away.
Degraded = guidance commands zero velocity (safe default).

Why timing matters, practice problem D2: the vehicle yaws at 45 deg/s
(search scan rate). If you used an attitude 50 ms too old, the error is
45 * 0.05 = 2.25 deg. In pixels near the center: 806 * tan(2.25 deg) =
about 32 px. A whole balloon at 10 m is only about 80 px wide.

Why the tracker should work in world angles (V-D10, planned in E2a-4):
today the tracker filters raw PIXELS. When the vehicle yaws, every pixel
slides even if the balloon is still. The constant-velocity model and the
box-overlap association break (probe finding V-S8: 39 plane detections
became 39 separate one-hit tracks). Fix: de-rotate each box center first,
then filter the world direction. The vehicle's own rotation cancels out.
And guidance gets the line-of-sight RATE (needed by PN) directly.

Lens distortion (V-D11, Brown-Conrady): real lenses bend straight lines.
Radial term: r_distorted = r * (1 + k1 r^2 + k2 r^4 + k3 r^6) on normalized
coordinates. The code undistorts box centers before using them. The
coefficients are zero until a ChArUco calibration is done.

## 6. Uncertainty: Gaussians, variance, covariance, fusion (scene 7)

Every measurement has noise. Model it as a Gaussian (bell curve) with a
mean (best guess) and a variance sigma^2 (spread; sigma = standard
deviation). About 68% of values fall within 1 sigma, 95% within 2 sigma.

Fusing two independent noisy estimates of the same thing: weight each by
1/variance (trust the precise one more):
  x_fused = (x1/s1^2 + x2/s2^2) / (1/s1^2 + 1/s2^2)
  1/s_fused^2 = 1/s1^2 + 1/s2^2   (variances combine like resistors in
  parallel; the fused one is ALWAYS tighter than either).
Same thing written as a correction:
  K = s1^2 / (s1^2 + s2^2);  x_fused = x1 + K (x2 - x1);
  s_fused^2 = (1 - K) s1^2.
That is exactly the Kalman update in one dimension.

Practice problem E: estimate A = 10 m with sigma 2 m; estimate B = 14 m
with sigma 4 m. Variances 4 and 16. K = 4/(4+16) = 0.2.
Fused = 10 + 0.2 * 4 = 10.8 m. Fused variance = 0.8 * 4 = 3.2, sigma 1.79 m.
(Common mistake: taking the plain average 12 m, which ignores that A is
four times more precise in variance.)

Covariance: for two variables (position and velocity), the covariance says
how their errors move together. Picture: a tilted ellipse in the
(position, velocity) plane. Covariance matrix P = [[p00, p01], [p01, p11]]:
p00 = position variance, p11 = velocity variance, p01 = how they co-vary
(positive tilt: if position is too high, velocity is probably too high).

Project rule worth a line: SkyWeave keeps two error channels: random error
(covariance, fed to filters) and systematic bias (for example a wrong
balloon width; reported, never folded into the covariance).

## 7. The Kalman filter in one dimension (scene 8)

Two-step dance that repeats every frame:
PREDICT (time passes, the model moves the estimate, uncertainty GROWS):
  x = x + r*dt;  P grows (we are less sure after time without looking).
UPDATE (a measurement z arrives, uncertainty SHRINKS):
  innovation y = z - x_predicted   (the surprise)
  S = P + R                         (expected size of the surprise; R =
                                     measurement variance)
  K = P / S                         (Kalman gain, between 0 and 1)
  x = x + K * y;  P = (1 - K) * P.
Meaning of K: K near 1 = trust the measurement (prediction was vague);
K near 0 = trust the prediction (sensor is noisy).

Code (`_Axis.update` in tracker.py): s = p00 + meas_var; k0 = p00 / s;
y = z - x; x += k0 * y. meas_sigma_px = 2 px, so R = 4 px^2.

Practice problem F: predicted center u = 510 px with variance 12 px^2.
Measured z = 520 px with variance 4 px^2.
y = 10; S = 16; K = 0.75; updated u = 510 + 7.5 = 517.5 px;
updated variance = 0.25 * 12 = 3 px^2 (sigma 1.73 px, tighter than the
sensor's 2 px).

## 8. The two-state Kalman filter: position AND velocity (scene 9)

We only measure position (box center), but we want velocity too. The trick
is the covariance p01. Matrix form:
  Predict: x = F x;  P = F P F^T + Q
  Update:  H = [1, 0] (we measure position only); S = H P H^T + R;
           K = P H^T / S = [p00/S, p01/S];
           x = x + K y;  P = (I - K H) P.
F P F^T explained visually: F shears the uncertainty ellipse (the
position uncertainty grows by the velocity uncertainty times dt). Written
out (matches `_Axis.predict` line by line):
  p00 += 2 dt p01 + dt^2 p11 + q dt^4/4
  p01 += dt p11 + q dt^3/2
  p11 += q dt^2
Q (process noise) = how much the real object might accelerate in ways the
model does not know. Model: a random constant acceleration over each step
with variance q = sigma_a^2. Over one step it moves position by a dt^2/2
and velocity by a dt, so Q = q * [[dt^4/4, dt^3/2], [dt^3/2, dt^2]].
Code: accel_sigma_px_s2 = 1000 px/s^2 for the center axes,
size_rate_sigma_px_s2 = 200 px/s^2 for width and height.

How velocity is learned: the gain's second entry k1 = p01/S. A surprise in
position also corrects velocity, because the filter knows position errors
and velocity errors are linked.

Practice problem G (stretch): dt = 0.1 s, before predict: u = 500, rate
100 px/s, p00 = 4, p01 = 0, p11 = 400 (rate sigma 20 px/s), q = 160000
(sigma_a = 400 px/s^2). Predict: u = 510; p00 = 4 + 0 + 0.01*400 +
160000*0.0001/4 = 4 + 4 + 4 = 12; p01 = 0 + 0.1*400 + 160000*0.001/2 =
40 + 80 = 120. Measure z = 520, R = 4: S = 16, k0 = 0.75, k1 = 7.5 per s.
u = 517.5 px; rate = 100 + 7.5*10 = 175 px/s. The filter reads the 10 px
surprise as "it is probably moving faster than I thought".

Tuning trade-off (the heart of filter design): big Q or small R = fast,
responsive, jittery. Small Q or big R = smooth, but lags a maneuver.
The ratio matters more than the values.

Coasting: on a miss (no detection), the filter only predicts. P grows each
frame, so the track's predicted box gets less certain. After coast_cap = 20
missed frames (one third of a second at 60 fps) the track dies.

## 9. Tracking many objects: association, gating, track lifecycle (scene 10)

Each frame has several boxes (balloon, bird, false alarm). Which box
belongs to which track? This is data association.
- IoU (intersection over union): overlap area / combined area of the
  predicted box and the detected box. 1 = identical, 0 = no overlap. The
  tracker pairs greedily by highest IoU, never below iou_min = 0.1
  (`Tracker.update`, `_iou`).
- Practice problem H: two 100 x 100 px boxes, one shifted 50 px right.
  Intersection 50 x 100 = 5000. Union 10000 + 10000 - 5000 = 15000.
  IoU = 1/3.
- Weakness: a fast or rotating view moves the box more than its own width
  between frames, IoU = 0, new track every frame (V-S8).
- Better gate (planned, E1-D2 trigger fired): Mahalanobis distance
  d^2 = y^2 / S, the surprise measured in units of the expected surprise.
  d^2 < 9 means within 3 sigma. It uses the filter's own uncertainty, so a
  coasting track (big S) accepts a wider area.
Track lifecycle (M-of-N, coasting):
- New box with no match: tentative track.
- Confirmed after 3 hits within its first 5 frames (m_confirm = 3,
  n_confirm = 5). This rejects one-frame false alarms.
- A confirmed track that misses: "coasting" (predict only).
- Dies after more than 20 consecutive misses; its id is never reused.
Mission link: SEARCH -> ACQUIRING when a confirmed track appears;
ENGAGED -> COASTING when the engaged track coasts; COASTING -> LOST when
it dies.

## 10. Nonlinear estimation: the EKF and the tangent plane (scene 11)

The Kalman filter assumes linear models (matrices). Real geometry is not
linear: angles, projections, rotations. Extended Kalman filter (EKF):
linearize around the current estimate. The tool is the Jacobian, the
matrix of partial derivatives (Calc 3), which is the multi-variable version
of "the derivative is the slope of the tangent line". Near the estimate,
a curved function looks like its tangent plane: h(x) ~ h(x0) + J (x - x0).
Use J in place of F or H in the covariance formulas.

Where EKFs appear here:
- ArduCopter's EKF3 estimates attitude, velocity and position. The gyro
  integrates fast but drifts. The accelerometer and GPS do not drift but
  are noisy. The EKF fuses them. Its output is the ATTITUDE stream SkyWeave
  uses for de-rotation.
- The Jetson multi-camera fusion line (parked right now) triangulates and
  runs an EKF track.

The planned world-angle tracker (E2a-4) uses a tangent-plane trick instead
of a full EKF: do not filter azimuth and elevation, because with the camera
looking straight up the view is centered on the zenith, where azimuth is
singular (like longitude at the North Pole: all meridians meet and a tiny
move changes longitude wildly). Instead, each track keeps a reference
direction and maps each line of sight onto a flat plane touching the unit
sphere at that reference (gnomonic projection: the point where the ray hits
the plane). Near the reference the plane is an excellent local map, so the
simple linear filters work there. Re-center if the track drifts far.

## 11. Range from size, and time to contact (scene 12)

Size range [G2] (`guidance.size_range`): similar triangles.
w (pixels) / f = W (meters) / Z (meters)  ->  Z = f W / w.
W = primed balloon width (for example 1 m). Range along the ray
r = Z * |ray|. Below w_min_px = 4 px the box is too small: assume
r = 15 m (clamped to 1..60 m) and label it "assumed".

Practice problem I: f = 806, W = 1 m.
(1) w = 403 px -> Z = 806/403 = 2 m. (That is the planned hold distance,
    V-D4: the balloon fills 0.21 of the frame width.)
(2) w = 80.6 px -> Z = 10 m.
(3) Sensitivity (Calc 1 derivative): dZ/dw = -f W / w^2, so
    dZ/Z = -dw/w. A 2 px width error at 10 m is 2/80.6 = 2.5% = 0.25 m.
    At 2 m it is 2/403 = 0.5% = 1 cm. Range gets better as you close.
(4) Systematic error: if the real balloon is 0.8 m but you primed 1 m,
    every range is 25% too long. Averaging more frames does NOT fix a bias.

Time to contact (V-D9, planned field on the track packet, E2a-5):
  tau = w / (dw/dt).
Derivation with the chain rule: w = f W / Z, so
dw/dt = -(f W / Z^2) dZ/dt. Divide: w / (dw/dt) = Z / (-dZ/dt)
= range / closing speed = seconds until contact.
f and W cancel! tau needs no calibration and no balloon size. Biology:
this is how a looming object triggers a blink (insects and birds use it).
The tracker's width filter already estimates dw/dt (the rate state).
Valid only while the box grows and is not clipped at the frame edge.
Uses: hand-off timing (stop steering when tau < about 0.2 s, V-D5), and a
cross-check of size range (a mismatch means the primed W is wrong).

Practice problem J: w = 403 px and dw/dt = 806 px/s. tau = 0.5 s. With
Z = 2 m, closing speed = 2/0.5 = 4 m/s. When does tau reach the 0.2 s
hand-off at 4 m/s? At 0.8 m.

## 12. Guidance laws: pure pursuit vs proportional navigation (scene 13)

Pure pursuit (today, [G3]): always fly toward where the target is now (here
toward a standoff point near it). Simple and stable. Against a moving
target it ends in a curved tail chase with high turn rates near the end.

Collision triangle (sailors' rule): if the BEARING to another ship stays
constant while the range shrinks, you will collide. In 3D: if the line of
sight (LOS) direction does not rotate, you are on a collision course.

Proportional navigation (planned PN pass, V-D2, E2b): command an
acceleration perpendicular to the LOS proportional to how fast the LOS
rotates:
  a_cmd = N * V_c * (d lambda / dt)
N = navigation constant (typically 3 to 5), V_c = closing speed,
d lambda/dt = LOS rotation rate (rad/s). PN drives the LOS rate to zero,
which puts you on the collision triangle, leading a moving target instead
of chasing it. Missiles use it; it needs only angles and their rates,
not the target's full position. That is why E2a builds the world-angle
tracker first: it gives d lambda/dt with the vehicle's own rotation
removed. V_c can come from tau (V_c = range / tau) or from size range.

Practice problem K: N = 3, V_c = 4 m/s, LOS rate 0.1 rad/s (5.7 deg/s).
a_cmd = 3 * 4 * 0.1 = 1.2 m/s^2. Tilt for that: atan(1.2/9.81) = 7 deg.

The planned pass (V-D5, V-D12): steer with PN at 50 Hz until the box
touches the frame edge, the track is lost, or tau < 0.2 s; then hold the
last command, then brake, climb, report pass_done. A short detection
dropout holds the last command up to coast_cap; if the track dies, brake,
climb, MISS.

Deferred: MPC (model predictive control) only if a recorded case shows PN
failing (V-D8).

## 13. Discrete time, latency, and the mission state machine (scene 14)

Rates in the system: camera 60 Hz; attitude about 50 Hz; guidance setpoints
10 Hz (planned 50 Hz in the pass); ArduCopter inner loops hundreds of Hz.
Latency: the picture is old by the time we act (about 27 ms detector
pipeline in the sim, plus transport). Delay is dangerous in feedback: you
correct for an error that already changed, so you overshoot. Intuition: a
shower with a long pipe; you turn the knob, nothing happens, you turn more,
then it is scalding. Fixes: lower gain, faster rates, predict forward with
the model (the Kalman predict step), time-stamp everything (t_cap), and
stop steering when the information gets unreliable (tau hand-off).
Also: ArduCopter jerk-limits acceleration inputs (PSC_JERK_NE), which adds
lag; and a setpoint that stops arriving for GUID_TIMEOUT = 3 s is zeroed.

Determinism: the mission and guidance never read a clock. Time comes only
from the stamps on their inputs, so a recorded flight replays exactly.

Mission state machine (`drone/mission.py`, contract section 4): discrete
logic on top of continuous control (a "hybrid system"). Main path for a
touch trial: PRIMED -> LAUNCH -> SEARCH (slow yaw scan at 45 deg/s, hold
altitude) -> ACQUIRING (a confirmed track appears; yaw toward it) ->
ENGAGED (operator approves; pursuit) -> TOUCH (commit gate passes) ->
COMPLETE or MISS -> RETURN -> LAND. Side paths: ENGAGED <-> COASTING
(misses), LOST (track died) -> SEARCH, ABORT (operator or radio switch)
-> RETURN, and budget or link-loss failsafes -> RETURN. Each state picks a
control law: SEARCH = altitude hold + constant yaw rate; ACQUIRING = yaw
law; ENGAGED = pursuit; COASTING = zero velocity + yaw on the prediction;
TOUCH = terminal segment.

Commit gate [G4] (touch trials): commit only when the box is big enough
(w/1920 >= alpha), centered enough (distance from center <= beta*1920),
the track is confirmed with at least k consecutive hits, and attitude is
fresh. Estimation quality gates the control decision.

## 14. Recap (scene 15)

Redraw the loop and label each arrow with its concept:
camera (pinhole, distortion) -> detector -> tracker (Kalman predict/update,
covariance, association, M-of-N, coasting) -> de-rotation (rotation
matrices, attitude at t_cap, EKF3 attitude) -> range (similar triangles)
and tau (chain rule) -> mission (state machine) -> guidance (P control,
exponential decay, clamps, rate limits; PN with LOS rate) -> ArduCopter
(cascaded PID, gravity feedforward) -> motors (Newton) -> back to camera.
Close with the core idea: estimation tells control what is true and how
sure we are; control acts; acting changes what we see.

## Suggested scene list (target 30+ minutes, about 2 minutes each)

s01 The loop: sense, estimate, decide, act (SkyWeave pipeline, open vs
    closed loop)
s02 State and differential equations (state vector, Euler step, F matrix,
    practice A)
s03 Proportional control and exponential decay (Kp, time constant, clamps,
    rate limit, practice B and B2)
s04 Newton, PID and the cascade inside the flight controller (double
    integrator, damping, eigenvalues, gravity feedforward, practice C, C2)
s05 Frames and rotation matrices (NED, body, camera, roll/pitch/yaw, the
    cone_up matrix)
s06 The pinhole camera and de-rotation (ray, f = 806, t_cap timing, world
    angles, practice D, D2)
s07 Uncertainty and fusing two guesses (Gaussian, variance, inverse
    variance weighting, covariance ellipse, practice E)
s08 The Kalman filter in 1D (predict/update, gain, practice F)
s09 The 2-state Kalman filter (F P F^T + Q, learning velocity, tuning,
    coasting, practice G)
s10 Tracking many objects (IoU, Mahalanobis, M-of-N, coasting, practice H)
s11 Nonlinear estimation (Jacobian, tangent plane, EKF3, gnomonic tracker)
s12 Range from size and time to contact (similar triangles, sensitivity,
    bias, tau by chain rule, practice I, J)
s13 Pursuit vs proportional navigation (collision triangle, PN law,
    practice K, the planned pass)
s14 Time, delay and the mission state machine (rates, latency, commit
    gate, states)
s15 Recap: the whole loop, every arrow labeled
