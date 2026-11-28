"""percepd v1 (DRONE_CONTRACTS_D0.md §11.4 B-D2): camera -> detector -> [P1].

The process reads camera frames, runs an object detector, and sends one [P1]
detection packet per processed frame to the companion's detection port
([C10], contract §9: UDP 14601). It is written new from the contract; the v0
source is missing (V-F4).

Modules, pure logic first:

- ``geometry``: letterbox math, NMS, the [C2] flight-grid mapping.
- ``layouts``: one decoder per model output layout.
- ``detect``: detections, thresholds, class filter, the backend interface.
- ``packet``: detections -> the frozen ``DetectionPacket``.
- ``sources``: camera, video file and image sources; the ``t_cap`` stamp.
- ``onnx_backend``: ONNX Runtime on the CPU (optional extra ``percepd``,
  imported lazily).
- ``viewer``: the bench picture (off by default).
- ``pipeline``: the keep-latest loop, counters, stats.
- ``cli``: ``python -m skyweave2.drone.percepd``.

README.md in this folder has the model, the commands and the limits.
"""
