"""Closed-loop harness for the drone stack (DRONE_CONTRACTS_D0.md §8, brief work item 5).

The loop is: synthetic target trajectory -> camera projection -> detection
packets -> tracker -> companion core -> fc_link -> ArduCopter SITL, closed
through the SITL vehicle's pose ([S0]). This package holds the pure parts of
that loop, which never touch a socket or a clock:

- ``targets``: truth trajectories in local NED (balloon, kite, thrown plane,
  crossing bird), closed forms only.
- ``camera_sim``: the synthetic camera. It projects truth through the real
  camera model with an injected boresight error and produces seeded, clipped
  detection packets, together with the noise-free truth the scorer needs.
- ``seeds``: the [S7] gate and probe seed rule.
- ``scorecard``: the [S8] metrics, the safety floors, and pass/fail per check
  from one run's trace, as canonical JSON with no wall-clock values.

Every random draw comes from an explicit seed, and every time is board ms
supplied by the caller ([C1]). The world's numbers are Provisional (E1) model
inputs. They are tuned only on probe seeds, inside a campaign file ([S7]).
"""
