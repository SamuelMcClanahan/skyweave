"""TransportConfig: the operational knobs of the wire path.

The split is deliberate. Values that ARE the freeze — the 1200 B ceiling, the
magic bytes, the wire version, the declared field bounds — live in
``wire.py`` as constants, because a config field implies "tune me" and none of
them may be tuned without a recorded decision. Everything here is
operational: timeouts, replay speed, and the loopback jitter budget the
report scores against.

Every value is Provisional. The loopback jitter budget in particular is a
HOST-LOOPBACK number and says nothing about the real switch: D9 measures
that.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Replay pacing.
    replay_speed: float = Field(default=1.0, gt=0.0)
    # Speed the multi-process rig replays at. DERIVED, not chosen.
    #
    # Accelerated replay is not free: at speed S, a D-millisecond scheduling
    # hiccup on the host looks like D*S milliseconds of CAPTURE-TIME lag to the
    # aligner, because the capture timestamps still carry the original 1x
    # spacing. The aligner's window is AlignerConfig.lateness_ns = 100 ms, so
    #
    #     safe speed <= lateness_ms / (worst host jitter ms * safety factor)
    #
    # With the declared jitter budget of 10 ms p95 and a 10x factor covering
    # the observed max-to-p95 ratio (~8x under load), the bound is
    # 100 / (10 * 10) = 1.0 — so the rig replays at REAL TIME. That is also
    # the D8/D9 condition, which makes it the honest default rather than a
    # concession: accelerating buys test seconds and spends alignment margin.
    # Running faster does not break the wire; it makes the aligner correctly
    # reject the trailing cameras, and the failure then reads as a parity
    # break when it is really a harness artifact.
    # `test_the_rig_replay_speed_leaves_room_for_host_jitter` re-derives this.
    rig_replay_speed: float = Field(default=1.0, gt=0.0)
    rig_speed_safety_factor: float = Field(default=10.0, gt=0.0)
    # Head start before the shared replay epoch, per edge process, so process
    # spawn cost lands before the clip rather than inside it as camera skew.
    spawn_margin_s: float = Field(default=2.0, ge=0.0)

    # Ingest loop. Wall-clock only; never enters a scored artifact.
    idle_timeout_s: float = Field(default=0.2, gt=0.0)
    max_idle_polls: int = Field(default=8, ge=1)
    drain_deadline_s: float = Field(default=180.0, gt=0.0)

    # Declared budgets for the WALL_CLOCK mode on host loopback (W6).
    # Scheduling error is what the pacer achieved against its own schedule;
    # packet age is receive time minus the scheduled capture instant, so it
    # contains the scheduling error plus the transit.
    #
    # These gate the DISTRIBUTION, not the tail, and that is deliberate. macOS
    # and Linux are not real-time systems: a single scheduler preemption of
    # tens of milliseconds is ordinary on a loaded host and says nothing about
    # the wire. Measured here at 1x-equivalent on this host: jitter p95 1.9 ms
    # quiet / 4.9 ms under a heavy parallel build; packet age p95 2.3 ms quiet
    # / 6.3 ms loaded. The p95 budgets are set with roughly 2x headroom over
    # the LOADED figure so the gate fails on a transport defect rather than on
    # a busy machine. The project gates p95 everywhere else (range p95, cross
    # p95) for the same reason.
    loopback_schedule_jitter_p95_ms: float = Field(default=10.0, gt=0.0)
    loopback_packet_age_p95_ms: float = Field(default=15.0, gt=0.0)
    # A PATHOLOGY bound, not a performance bound: it exists to catch a stalled
    # sender or a datagram released far out of band, and is set well above any
    # plausible scheduler hiccup. The observed maximum is always REPORTED, so
    # a degrading tail is visible even while this gate holds.
    loopback_packet_age_max_ms: float = Field(default=250.0, gt=0.0)

    # Kernel receive buffer for the measurement socket. Sized so three edge
    # nodes bursting a frame period apart cannot lose datagrams to buffer
    # pressure — otherwise the rig measures the harness, not the system.
    rcvbuf_bytes: int = Field(default=4 * 1024 * 1024, gt=0)


TRANSPORT_CONFIG = TransportConfig()
