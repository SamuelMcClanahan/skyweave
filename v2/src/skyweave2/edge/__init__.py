"""Edge-node integration: the C1 injection harness and the D8 fixtures.

Everything the RV1106 daemon needs from the host lives here. The daemon
itself is C under ``v2/firmware/rv1106/``; this package is its counterpart:
the harness that feeds it luma frames, the fixture builder that turns known
clips into expected observations and expected packet bytes, and the report
generator.

The host detector remains the ORACLE. Nothing in this package re-derives a
detection; it runs ``skyweave2.detector`` and records what came out.
"""

from skyweave2.edge.injection import (
    INJECTION_MAGIC,
    INJECTION_VERSION,
    InjectionSession,
    InjectionStreamError,
    PtsProfile,
    build_injection_stream,
    fabricate_pts,
    iter_injection_frames,
    read_injection_session,
)

__all__ = [
    "INJECTION_MAGIC",
    "INJECTION_VERSION",
    "InjectionSession",
    "InjectionStreamError",
    "PtsProfile",
    "build_injection_stream",
    "fabricate_pts",
    "iter_injection_frames",
    "read_injection_session",
]
