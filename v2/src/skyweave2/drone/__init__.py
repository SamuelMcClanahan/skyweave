"""SkyWeave drone stack, phase E1 (pre-writable software, SITL only).

Contracts first: ``docs/DRONE_CONTRACTS_D0.md``. The wire is ``packets.py``;
whole-flight recording is ``recording.py``. No code path in this package can
arm or command a real flight controller ([F1]).
"""
