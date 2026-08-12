"""The D8.1 board benchmark, driven from the host.

What D8.1 has to produce is a table: GMM2 at 2304x1296, 1536x864 and 1152x648,
each with sustained fps, memory, DDR bandwidth, A7 utilisation and thermals,
plus a one-hour soak at whichever resolution survives. This module is the
runner that produces it, and — per the D8.1-prep amendment — it is written and
exercised against the HOST-built daemon first, so the first board session
debugs the board rather than this file.

Three things it does NOT do, each on purpose:

- **It does not choose the deployment resolution.** The sweep produces numbers;
  the intersection argument in the report chooses, and the choice becomes a
  decisions-log entry. A runner that also picked would be grading its own work.
- **It does not invent a number it cannot read.** Every metric arrives as a
  :class:`~skyweave2.edge.metrics.Measurement`, which is either a value with the
  mechanism that produced it or NOT-MEASURED with the reason. DDR bandwidth is
  NOT-MEASURED on every host and says so.
- **It does not pace itself unless told to.** A sweep run is unpaced: frames go
  in as fast as the source can deliver them, and the resulting fps is a
  throughput CEILING. A soak run is paced at a declared rate, and its fps is a
  duty cycle. These are different quantities and the artifact says which one it
  holds.

**The source is part of the measurement.** A sweep at 2304x1296 needs 2.99 MB
per frame, which is 90 MB/s at 30 fps. A 100M link carries about a tenth of
that and an SD card about a fifth, so an injected benchmark can easily measure
the pipe rather than GMM2. :data:`SOURCE_MEDIA` records the arithmetic and every
run records the byte rate it achieved, so a source-bound run is visible in the
artifact instead of being quoted later as a detector ceiling. Finding D8-F8 in
the D8 report states the consequence for the board session.

The scene is the fixture scene's arithmetic at a different size: same seeded
movers, same background, sizes and speeds quoted at the FULL sensor grid and
scaled onto whichever processing grid a run uses. It is deliberately NOT the
committed fixture clip — that one is 640x480 and its bytes are a byte-identity
gate, and a benchmark must never be able to move a gate fixture.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path

import numpy as np

from skyweave2.contracts import ClockDomain
from skyweave2.detector.config import Backend, DetectorConfig
from skyweave2.edge import daemon, health, metrics
from skyweave2.edge.injection import (
    InjectionFrame,
    InjectionSession,
    PtsProfile,
    encode_frame_record,
    encode_session_header,
    encode_trailer,
    fabricate_pts,
)
from skyweave2.edge.tolerance import BENCHMARK_RUN_TO_RUN, BenchmarkTolerance

#: The three resolutions D4 swept and scored on the host. All three passed the
#: D4 gate, so the board ceiling is what picks one; these are the rows the
#: report has been carrying as PENDING since D8.0.
D4_RESOLUTIONS = ((2304, 1296), (1536, 864), (1152, 648))

#: The sensor grid every run DECLARES, whatever grid it processes on. The
#: injected frames are already at processing resolution (C1 injects
#: pre-scaled), and the session header carries the full grid so the daemon
#: applies the D0 scaling law exactly as it would on a node.
FULL_WIDTH = 2304
FULL_HEIGHT = 1296

#: What a benchmark frame contains. Quoted at FULL resolution and scaled onto
#: the processing grid, so the same scene at three resolutions is the same
#: scene rather than three different ones.
SCENE_SEED = 7
SCENE_MOVERS = 6
SCENE_BACKGROUND_DN = 96.0
SCENE_NOISE_DN = 2.0
SCENE_BLOB_AMPLITUDE_DN = 90.0
SCENE_BLOB_SIGMA_FULL_PX = 12.0
SCENE_SPEED_FULL_PX_PER_FRAME = 11.0

#: Six movers against a cap of seven: the cap never bites, so a sweep measures
#: the DETECTOR's cost and not the cap's selection. A benchmark that ran at the
#: cap would report the cost of dropping components as if it were the cost of
#: finding them.
CAP_HEADROOM_NOTE = "6 movers against a cap of 7: the cap is inert by construction"

#: What each medium can actually deliver, and what each resolution needs at
#: 30 fps. Not measured here — these are the declared planning numbers from the
#: node design (100M Ethernet, "~90 Mbit/s usable") and the ordinary range for
#: a class-10 SD card. They exist so a run's achieved byte rate can be read
#: against something.
SOURCE_MEDIA = {
    "100m-ethernet": 11.25,
    "sd-card-class10": 20.0,
    "host-page-cache": 2000.0,
}

#: The four source kinds the daemon can run, spelled exactly as
#: ``sw_source_name()`` in ``firmware/rv1106/src/sw_config.c`` spells them and
#: exactly as the daemon's own ``--stats`` reports them. Two spellings of one
#: source mode would make a collected node artifact unreadable against a host
#: one, which is the gap ``source_mode`` in ``--stats`` exists to close.
SOURCE_MODE_INJECT_FILE = "inject-file"
SOURCE_MODE_INJECT_TCP = "inject-tcp"
SOURCE_MODE_INJECT_RAM = "inject-ram"
SOURCE_MODE_CAPTURE_VI = "capture-vi"
SOURCE_MODES = (
    SOURCE_MODE_INJECT_FILE,
    SOURCE_MODE_INJECT_TCP,
    SOURCE_MODE_INJECT_RAM,
    SOURCE_MODE_CAPTURE_VI,
)

#: The RAM-loop budget the harness derives clip lengths against, in DECIMAL
#: bytes (10^6 per MB). It is the upper end of the with-NPU subtotal in
#: `v1/docs/RV1106_EDGE_NODE.md` section 6, "~120-160 MB" — read as a
#: DAEMON-ONLY ceiling, which is a narrower thing than that subtotal, since
#: that subtotal also counts its own "~30-50 MB" kernel+rootfs row. Section 6
#: states NO numeric margin anywhere: its Notes cells read "still fits with
#: margin" and "very comfortable", which is prose. Which reading of "160 MB"
#: governs is an open question for the planning session; this is the default
#: the daemon ships behind ``--ram-budget-mb``.
RAM_LOOP_BUDGET_BYTES = 160_000_000

#: Declared systematic, recorded beside every RAM-loop result. It is folded
#: into no bound and appears in no tolerance table — a DDR read of a resident
#: clip is not an ISP write, and pretending the difference is covered by a
#: tolerance would be folding a bias into a random error.
DDR_PROFILE_NOTE = (
    "RAM-loop source: the clip is resident in DDR and re-read by the detector, "
    "so the DDR traffic profile differs from real ISP writes. Declared "
    "systematic (D0 decisions log, 'D8.1 opening', F8 resolution); recorded "
    "beside every result and never folded into any bound."
)

#: The other declared systematic, and the one that changes what an fps number
#: MEANS rather than where the bytes came from.
RAM_LOOP_SCENE_NOTE = (
    "RAM-loop scene: the clip wraps, so the movers return to their frame-0 "
    "positions and the background model meets pixels it has already seen. "
    "frame_seq stays continuous across every wrap and capture_ts_ns advances "
    "by a per-pass stride the harness declares, so warm-up runs exactly once "
    "and no timestamp repeats. The clip is built with movers present from "
    "frame 0 — a clip shorter than the plan's warm-up would otherwise be pure "
    "background — so the detector's warm-up window contains movers, unlike a "
    "file-source sweep that warms on a moverless lead-in. Declared systematic; "
    "recorded beside every result and never folded into any bound."
)


def frame_bytes(proc_width: int, proc_height: int) -> int:
    return proc_width * proc_height


def required_mb_s(proc_width: int, proc_height: int, fps: float) -> float:
    """What feeding this grid at this rate costs, in MB/s. Plain arithmetic,
    stated so nobody has to redo it in their head at a bench."""
    return frame_bytes(proc_width, proc_height) * fps / 1e6


# ---------------------------------------------------------------------------
# RAM-loop budget arithmetic. DERIVED, never Measured.
# ---------------------------------------------------------------------------
#
# Every function below is arithmetic over allocator sizes and clip geometry. No
# reading of any kind reaches them, on any machine, so nothing they return may
# ever wear a Measured label. The one measured memory number in a run record is
# peak RSS — and on a board that does not even contain the IVE detector's
# share, which comes out of MMZ/CMA rather than the process heap.


#: ``ive_footprint_for``'s third term, ``sizeof(IVE_CCBLOB_S)``, which the arm
#: below does NOT count — declared here with its reason rather than left as a
#: silent zero, and used by
#: ``test_the_omitted_ive_blob_term_cannot_move_a_derived_clip_length`` to prove
#: the omission is inert at the D4 grids.
#:
#: A DECLARED UPPER BOUND on an SDK type, not a transcription: ``IVE_CCBLOB_S``
#: appears nowhere in this checkout (it is a ``rk_mpi_ive.h`` type), and the IVE
#: arm compiles to a refusal without ``SKYWEAVE_HAVE_RKMPI``, so nothing on this
#: side of the board can read its real size. What IS known here is the shape:
#: one blob holds at most ``SW_MAX_COMPONENTS`` = 254 regions
#: (``IVE_MAX_REGION_NUM``, ``include/sw_detect.h``), each an area word and four
#: edge coordinates, plus a small header — kilobytes, and this bound is an order
#: of magnitude above that. Chosen, never Measured; the board's real value
#: arrives with C3, which is also the first run that executes this arm at all.
IVE_BLOB_DECLARED_BOUND_BYTES = 65_536


def detector_state_bytes(
    proc_width: int, proc_height: int, detector: str = "ive", model_num: int = 3
) -> int:
    """What the named backend passes to its allocator for this grid.

    ``soft`` mirrors ``soft_footprint_for`` in ``sw_detect_soft.c`` exactly
    (46 B/px at model_num 3), and that equality is not a claim: the daemon
    prints its own footprint and
    ``test_the_budget_check_refuses_rather_than_fitting_the_number`` asserts
    this function's number appears in it, off a real host run.

    ``ive`` mirrors ``ive_geometry`` / ``ive_footprint_for`` in
    ``sw_detect_ive.c`` for the first two of the C's three terms — stride
    rounded up to 16, four U8 planes, plus 12 bytes per model per pixel — and
    DELIBERATELY OMITS the third, ``sizeof(IVE_CCBLOB_S)``. The omission is
    declared (:data:`IVE_BLOB_DECLARED_BOUND_BYTES`) rather than silent, and the
    reason it cannot be closed here is that the type is an SDK header's, absent
    from this checkout, on an arm that does not compile off the node. Nor is
    this arm pinned against the daemon: nothing host-side runs it, so its number
    is checked against the C only by reading the C.

    So the ive number is a LOWER BOUND on what the board's daemon will sum, and
    the daemon refuses rather than clamps when a clip is over. At the three D4
    grids the budget slack the derived row leaves is larger than the declared
    blob bound, so counting the blob would neither shorten a clip nor break a
    fit — and that is asserted by
    ``test_the_omitted_ive_blob_term_cannot_move_a_derived_clip_length``, at the
    grids and budget the report publishes, rather than asserted in this
    sentence. It holds at those grids and nowhere by construction; a new grid or
    a smaller budget re-opens the question and the test is where it re-opens.
    Both claims came out of the finding 'Mirrors the C exactly and the two are
    pinned equal by a test are both false for the IVE arm'.

    This exists so the harness can DERIVE a clip length before it starts a
    process; the daemon computes its own from the allocator it actually calls.
    """
    if detector == "ive":
        stride = (proc_width + 15) & ~15
        plane = stride * proc_height
        return 4 * plane + plane * model_num * 12
    if detector == "soft":
        # 3 float planes per model, then fg, match, label and area.
        return proc_width * proc_height * (model_num * 3 * 4 + 1 + 1 + 4 + 4)
    raise ValueError(f"no allocator is known for detector {detector!r}")


def ram_clip_bytes(proc_width: int, proc_height: int, clip_frames: int) -> int:
    """The arena a preloaded clip occupies: luma only, one contiguous block."""
    return frame_bytes(proc_width, proc_height) * clip_frames


#: The five ``sizeof()`` terms in the daemon's ``fixed`` — components, event,
#: datagram, pipeline, config (``main.c``'s RAM budget block) — as a DECLARED
#: UPPER BOUND, Chosen and never Measured.
#:
#: An upper bound rather than a number, because struct layout is a property of
#: the target ABI: this host prints 21,216 B and the board's ARM32 build will
#: print something else, and a Python transcription of five C structs would be
#: a second copy of the daemon's arithmetic — the exact shape the C's own
#: comment in ``sw_detect_ive.c`` forbids ("the budget check could pass against
#: a formula that is not the one that runs"). A bound has the property a copy
#: does not: as long as it is >= the daemon's own residue, a clip the harness
#: derives is a clip the daemon accepts. That inequality is not assumed — it is
#: read off the daemon's own printed ``fixed`` by
#: ``test_the_declared_struct_allowance_bounds_the_daemons_own_fixed_term``,
#: which is a harness-vs-DAEMON gate and not harness-vs-a-second-copy.
#:
#: The board's residue is NOT-MEASURED until C3 prints it; the host's is what
#: this bound is currently proven against, and section 8 says so.
DAEMON_STRUCT_ALLOWANCE_BYTES = 65_536


def daemon_fixed_bytes(proc_width: int, proc_height: int) -> int:
    """The daemon's ``fixed`` term, bounded from above.

    ``main.c``'s budget check is ``clip + detector + fixed > budget``, and
    ``fixed`` is five struct sizes plus ``inject.luma_capacity`` — which
    ``sw_inject.c``'s ``read_session`` sets to one full frame, ``proc_width *
    proc_height``, on every injection open including ``--inject-ram``.

    The luma half is exact and target-independent. The struct half is the
    declared bound above. The harness omitted this term entirely until the
    finding "Section 8's RAM-budget table is not the sum the daemon enforces":
    at 1152x648 the published 174-frame row left 249,856 B of headroom while
    the luma term alone is 746,496 B, so the daemon refused the clip the report
    said fitted, on every platform.
    """
    return DAEMON_STRUCT_ALLOWANCE_BYTES + frame_bytes(proc_width, proc_height)


def _period_exact(clip_frames: int, fps: float) -> bool:
    """Whether ``clip_frames`` frames at ``fps`` is a whole number of ns.

    Fraction, not float: the question is exactly whether an integer exists, and
    asking it in binary floating point is asking a different question.
    """
    return (Fraction(clip_frames) * 10**9 / Fraction(fps)).denominator == 1


def ram_loop_max_frames(
    proc_width: int,
    proc_height: int,
    detector: str = "ive",
    budget_bytes: int = RAM_LOOP_BUDGET_BYTES,
    fps: float = 30.0,
) -> int:
    """The longest clip that fits the budget AND has an exact per-pass stride.

    The second condition is a CORRECTNESS requirement and not tidiness. The
    looped ``capture_ts_ns`` equals what an unrolled feed would have carried
    only when ``clip_frames * 1e9 / fps`` is a whole number of nanoseconds:
    with it, ``round((pass*N + i)/fps*1e9) == pass*stride + round(i/fps*1e9)``
    for every pass and slot; without it the two diverge by a rounding per pass
    and the loop stops reproducing the harness's own looping rule. At 30 fps
    that is why the full-res clip is 12 frames and not the 13 the byte budget
    alone would allow.

    The room is the budget less the detector's state AND less the daemon's
    ``fixed`` term (:func:`daemon_fixed_bytes`), because the daemon's check is
    ``clip + detector + fixed > budget``. Subtracting only the detector is what
    made the published 1152x648 row a clip the daemon refuses.

    Returns 0 when the detector's own state already spends the budget, which is
    a true answer and not an error: the caller refuses with the arithmetic.
    """
    room = (
        budget_bytes
        - detector_state_bytes(proc_width, proc_height, detector)
        - daemon_fixed_bytes(proc_width, proc_height)
    )
    per_frame = frame_bytes(proc_width, proc_height)
    count = room // per_frame if room > 0 else 0
    while count > 0 and not _period_exact(count, fps):
        count -= 1
    return max(0, count)


def ram_budget_row(
    proc_width: int,
    proc_height: int,
    clip_frames: int | None = None,
    detector: str = "ive",
    budget_bytes: int = RAM_LOOP_BUDGET_BYTES,
    fps: float = 30.0,
) -> dict:
    """One row of the RAM-budget arithmetic, with every term kept separate.

    Separate because a total that fails says nothing about which term to change,
    and because the detector's share and the clip's share are reclaimed by
    different mechanisms on the board.

    Three terms, not two: ``daemon_fixed_bytes`` is here because the daemon
    counts it, and a row whose ``total_bytes`` was ``state + clip`` published
    ``fits: True`` for a configuration the daemon refuses at startup.
    """
    if clip_frames is None:
        clip_frames = ram_loop_max_frames(
            proc_width, proc_height, detector, budget_bytes, fps
        )
    state = detector_state_bytes(proc_width, proc_height, detector)
    clip = ram_clip_bytes(proc_width, proc_height, clip_frames)
    fixed = daemon_fixed_bytes(proc_width, proc_height)
    return {
        "resolution": f"{proc_width}x{proc_height}",
        "detector": detector,
        "detector_state_bytes": state,
        "clip_frames": clip_frames,
        "clip_bytes": clip,
        "daemon_fixed_bytes": fixed,
        "total_bytes": state + clip + fixed,
        "budget_bytes": budget_bytes,
        "fits": state + clip + fixed <= budget_bytes,
        "basis": (
            "derived arithmetic from the backend's own allocator sizes, the "
            "clip geometry and a DECLARED upper bound on the daemon's fixed "
            "struct residue; NOT a measurement"
        ),
    }


def ram_budget_table(
    resolutions: tuple[tuple[int, int], ...] = D4_RESOLUTIONS,
    detector: str = "ive",
    budget_bytes: int = RAM_LOOP_BUDGET_BYTES,
    fps: float = 30.0,
) -> list[dict]:
    """The pre-declared CAPACITY table: every row is the DERIVED MAXIMUM.

    Attached to no run, by construction — ``clip_frames`` is ``None`` on every
    row, so each one answers "what is the longest clip that would fit here",
    not "what did anybody load". A record that describes runs must build its
    rows from the runs (:meth:`SoakResult.as_dict` does, and
    :meth:`SweepResult.as_dict` does now); substituting this table there
    published a clip nobody ran.
    """
    return [
        ram_budget_row(width, height, None, detector, budget_bytes, fps)
        for width, height in resolutions
    ]


@dataclass(frozen=True)
class BenchmarkPlan:
    """Everything a run needs to be reproducible, and nothing else.

    Every field is an INPUT. There is no field here that a result writes back
    into, so the plan can be committed beside the numbers it produced and the
    pair is a complete description of the run.
    """

    frames: int = 120
    warmup_frames: int = 30
    seed: int = SCENE_SEED
    movers: int = SCENE_MOVERS
    fps: float = 30.0
    camera_id: int = 0
    cap: int = 7
    #: Which medium fed the run, keyed into :data:`SOURCE_MEDIA`. Declared by
    #: the operator, never guessed: the harness cannot tell an SD card from a
    #: tmpfs, and a verdict about being source-bound is worthless if the
    #: denominator was assumed.
    source_medium: str | None = None
    #: Which source feeds the run, keyed into :data:`SOURCE_MODES`. Declared
    #: rather than inferred from whether a stream path was passed: three of the
    #: four modes are injection modes, so the inference is ambiguous, and it
    #: was being discarded before it could reach a collected artifact.
    source_mode: str = SOURCE_MODE_INJECT_FILE
    #: None means "derive the longest clip that fits the declared budget and
    #: has an exact per-pass stride". The derived value and the arithmetic that
    #: produced it both land in the run record.
    ram_clip_frames: int | None = None
    ram_budget_mb: int = 160
    # There is deliberately NO `ram_paced_fps` here. It existed, it was
    # serialised into both scored records, and no run path read it: `run_sweep`
    # built its declaration without a pace and `run_soak` used its own
    # `target_fps` argument, so a plan that declared 15 fps produced a record
    # whose plan block said 15 while `ram_plan.period_ns` said 33333333 and the
    # daemon ran at 30. The pace a soak actually applied is already stated three
    # ways that cannot drift from the run — `target_fps`, `run.paced_fps` and
    # `run.ram_plan["period_ns"]` — and a fourth unbound copy could only ever
    # disagree with them.

    def resolved_ceiling_mb_s(self) -> float | None:
        if self.source_medium is None:
            return None
        return SOURCE_MEDIA.get(self.source_medium)

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "warmup_frames": self.warmup_frames,
            "seed": self.seed,
            "movers": self.movers,
            "fps": self.fps,
            "camera_id": self.camera_id,
            "cap": self.cap,
            "source_medium": self.source_medium,
            "source_mode": self.source_mode,
            "ram_clip_frames": self.ram_clip_frames,
            "ram_budget_mb": self.ram_budget_mb,
        }


def benchmark_config(proc_width: int, proc_height: int, plan: BenchmarkPlan) -> DetectorConfig:
    """The detector config for one sweep row.

    ``ive_approx`` and every shipped default, exactly as the fixtures do: the
    sweep varies RESOLUTION and nothing else, so a difference between two rows
    is a difference in the grid. ``daemon.daemon_args`` refuses any other
    backend and asserts every GMM2 knob against the daemon's compiled-in
    values, which is what keeps that promise honest.
    """
    return DetectorConfig(
        backend=Backend.IVE_APPROX,
        proc_width=proc_width,
        proc_height=proc_height,
        warmup_frames=plan.warmup_frames,
        max_components_per_frame=plan.cap,
        fps=plan.fps,
        detector_rev="d8-bench/1",
        calibration_rev="d8-bench-cal",
    )


def _tracks(plan: BenchmarkPlan) -> list[tuple[np.ndarray, np.ndarray]]:
    rng = np.random.default_rng(np.random.SeedSequence([plan.seed, 0xBE4C]))
    tracks = []
    for _ in range(plan.movers):
        start = rng.uniform((80.0, 80.0), (FULL_WIDTH - 80.0, FULL_HEIGHT - 80.0))
        angle = rng.uniform(0.0, 2.0 * np.pi)
        velocity = SCENE_SPEED_FULL_PX_PER_FRAME * np.array([np.cos(angle), np.sin(angle)])
        tracks.append((start, velocity))
    return tracks


def iter_scene_frames(
    plan: BenchmarkPlan, proc_width: int, proc_height: int
) -> Iterator[np.ndarray]:
    """The benchmark scene, rendered directly on the processing grid.

    Rendered at proc rather than rendered full and downscaled, for a reason
    worth stating: a 2304x1296 render of 120 frames is 358 MB of intermediate
    and several minutes of numpy, which would put the harness's own cost inside
    a measurement of the daemon. The movers are specified at full resolution
    and divided by the scale, so the three grids see the same scene at three
    sampling densities — which is exactly what the sweep is asking about.

    Deterministic: the noise for frame N is keyed by (seed, N), never drawn
    from a running stream, so a partial run and a full one agree frame for
    frame. Same rule as the fixture scene.
    """
    scale_x = FULL_WIDTH / proc_width
    scale_y = FULL_HEIGHT / proc_height
    sigma_u = SCENE_BLOB_SIGMA_FULL_PX / scale_x
    sigma_v = SCENE_BLOB_SIGMA_FULL_PX / scale_y
    tracks = _tracks(plan)
    v_grid, u_grid = np.meshgrid(
        np.arange(proc_height, dtype=np.float32),
        np.arange(proc_width, dtype=np.float32),
        indexing="ij",
    )
    for frame_seq in range(plan.frames):
        rng = np.random.default_rng(
            np.random.SeedSequence([plan.seed, frame_seq, 0x0A15E])
        )
        image = SCENE_BACKGROUND_DN + rng.normal(
            0.0, SCENE_NOISE_DN, size=(proc_height, proc_width)
        ).astype(np.float32)
        if frame_seq >= plan.warmup_frames:
            moved = frame_seq - plan.warmup_frames
            for start, velocity in tracks:
                cu_full, cv_full = start + velocity * moved
                cu = cu_full / scale_x
                cv = cv_full / scale_y
                if not (0 <= cu < proc_width and 0 <= cv < proc_height):
                    continue
                image += SCENE_BLOB_AMPLITUDE_DN * np.exp(
                    -0.5 * (((u_grid - cu) / sigma_u) ** 2 + ((v_grid - cv) / sigma_v) ** 2)
                )
        yield np.clip(np.rint(image), 0, 255).astype(np.uint8)


def benchmark_session(
    plan: BenchmarkPlan,
    proc_width: int,
    proc_height: int,
    session_uuid: str,
    frame_count: int,
    profile: PtsProfile | None = None,
) -> InjectionSession:
    """The session header for a benchmark stream.

    ``profile`` is threaded in for ONE reason: the header's
    ``declaration_overridden`` flag. ``build_injection_stream`` has always set
    it from ``not profile.is_honest`` (injection.py), so a known-lie stream
    announces itself to every reader; this builder did not, so the same
    dishonest profile produced a clip whose header flag was CLEAR. That gap was
    invisible until the RAM-loop source began refusing OVERRIDDEN clips — the
    refusal is real in the daemon and was untestable from here, which is a
    declaration that only exists on one side of the wire.
    """
    config = benchmark_config(proc_width, proc_height, plan)
    return InjectionSession(
        camera_id=plan.camera_id,
        session_uuid=session_uuid,
        full_width=FULL_WIDTH,
        full_height=FULL_HEIGHT,
        proc_width=proc_width,
        proc_height=proc_height,
        frame_count=frame_count,
        fps=plan.fps,
        exposure_us=config.exposure_us,
        gain_db=0.0,
        line_readout_us=0.0,
        clock_domain=ClockDomain.SYNTHETIC,
        calibration_rev=config.calibration_rev,
        detector_rev=config.detector_rev,
        declaration_overridden=profile is not None and not profile.is_honest,
    )


def _frame_record(
    plan: BenchmarkPlan, profile: PtsProfile, luma: np.ndarray, frame_seq: int
) -> bytes:
    scene_ts_ns = int(round(frame_seq / plan.fps * 1e9))
    capture_ts_ns, declared_ms = fabricate_pts(
        scene_ts_ns, profile, plan.camera_id, frame_seq
    )
    return encode_frame_record(
        InjectionFrame(
            frame_seq=frame_seq,
            capture_ts_ns=capture_ts_ns,
            time_sync_error_ms=declared_ms,
            luma=luma,
        )
    )


def write_benchmark_stream(
    path: str | Path,
    plan: BenchmarkPlan,
    proc_width: int,
    proc_height: int,
    session_uuid: str,
    profile: PtsProfile | None = None,
    frame_count: int | None = None,
) -> int:
    """Write one sweep row's SWIJ stream to disk. Returns its size in bytes.

    Streamed record by record rather than joined in memory: at 2304x1296 the
    whole thing is 3 MB per frame, and a benchmark harness that needs the run
    resident to describe it would be measuring the wrong machine.

    ``frame_count`` writes a SHORTER clip than the plan runs — what a RAM loop
    needs, since the loop's length and the run's length are different numbers.
    The shadow plan it builds sets ``warmup_frames=0``, and that is
    load-bearing rather than incidental: :func:`iter_scene_frames` only draws
    movers once ``frame_seq >= plan.warmup_frames``, so a 12-frame clip built
    with the plan's own 30-frame warm-up would be PURE BACKGROUND — the sweep
    would be measuring GMM2 on flat noise, with capture_events 0 and a green
    suite. The daemon's own ``--warmup`` is untouched and still comes from
    ``plan.warmup_frames``, applied over the RUN's continuous frame_seq, so
    warm-up happens exactly once and spans several wraps. Determinism survives
    the shadow plan because ``iter_scene_frames`` keys its noise on
    ``(seed, frame_seq)`` and never on a running stream, so a short plan's
    frames are byte-identical to a long one's leading frames.
    """
    profile = profile or PtsProfile()
    path = Path(path)
    clip_plan = plan if frame_count is None else replace(
        plan, frames=frame_count, warmup_frames=0
    )
    session = benchmark_session(
        clip_plan, proc_width, proc_height, session_uuid, clip_plan.frames, profile
    )
    written = 0
    with path.open("wb") as handle:
        handle.write(encode_session_header(session))
        for frame_seq, luma in enumerate(
            iter_scene_frames(clip_plan, proc_width, proc_height)
        ):
            handle.write(_frame_record(clip_plan, profile, luma, frame_seq))
            written += 1
        handle.write(encode_trailer(written))
    return path.stat().st_size


def ram_loop_declaration(
    plan: BenchmarkPlan,
    proc_width: int,
    proc_height: int,
    total_frames: int,
    detector: str = "ive",
    paced_fps: float | None = None,
    profile: PtsProfile | None = None,
) -> daemon.RamLoopDeclaration:
    """Derive the clip length and the per-pass stride, or refuse with the reason.

    Every refusal here is a condition under which the daemon's constant-stride
    advance would stop reproducing what ``_feed_stream_over_tcp`` already does
    on this host. They are checked at the host because the host is where the
    clip is built and where the profile is known; the daemon separately refuses
    the clips it can see are wrong (frames not numbered 0..N-1, timestamps that
    do not increase, a stride no longer than the clip it advances).
    """
    if plan.source_mode != SOURCE_MODE_INJECT_RAM:
        raise ValueError(
            f"this plan declares source_mode {plan.source_mode!r}, so it is not "
            "asking for a RAM loop. Building one anyway would put a clip on a "
            "node that the run record says nothing about."
        )
    budget_bytes = plan.ram_budget_mb * 1_000_000
    derived_max = ram_loop_max_frames(
        proc_width, proc_height, detector, budget_bytes, plan.fps
    )
    arithmetic = (
        f"{detector} detector state "
        f"{detector_state_bytes(proc_width, proc_height, detector)} B + daemon "
        f"fixed {daemon_fixed_bytes(proc_width, proc_height)} B (one luma frame "
        f"plus a declared {DAEMON_STRUCT_ALLOWANCE_BYTES} B struct bound) + "
        f"{frame_bytes(proc_width, proc_height)} B per frame against a declared "
        f"{budget_bytes} B budget (--ram-budget-mb {plan.ram_budget_mb}, decimal "
        f"MB) leaves room for {derived_max} period-exact frames at "
        f"{plan.fps:g} fps"
    )
    clip_frames = plan.ram_clip_frames if plan.ram_clip_frames is not None else derived_max
    if clip_frames < 1:
        raise ValueError(
            f"no RAM-loop clip fits at {proc_width}x{proc_height}: {arithmetic}. "
            "Refusing rather than shortening the budget to obtain a pass."
        )
    if plan.ram_clip_frames is not None and plan.ram_clip_frames > derived_max:
        raise ValueError(
            f"ram_clip_frames={plan.ram_clip_frames} at {proc_width}x{proc_height} "
            f"does not fit: {arithmetic}."
        )
    if not _period_exact(clip_frames, plan.fps):
        raise ValueError(
            f"a {clip_frames}-frame clip at {plan.fps:g} fps is not a whole "
            "number of nanoseconds, so the per-pass PTS stride would not be "
            "exact and a looped replay would not carry the timestamps an "
            "unrolled replay carries. Pick a length for which "
            "clip_frames * 1e9 / fps is an integer."
        )
    profile = profile or PtsProfile()
    if profile.drift_ppm != 0.0:
        raise ValueError(
            f"a drifting profile (drift_ppm={profile.drift_ppm}) cannot be looped "
            "under a constant per-pass stride: the declared error grows with "
            "scene time, so pass 2 onwards would carry a timestamp no unrolled "
            "feed would have written."
        )
    if profile.jitter_ms_sigma != 0.0:
        raise ValueError(
            f"a jittering profile (jitter_ms_sigma={profile.jitter_ms_sigma}) "
            "cannot be looped: the draw is keyed by (seed, camera_id, "
            "frame_seq), so a repeated clip slot carries the wrong frame's "
            "perturbation from the second pass on."
        )
    if not profile.is_honest:
        raise ValueError(
            "this profile OVERRIDES its declared error, and a known-lie fixture "
            "may not become a looped sweep source. The daemon refuses such a "
            "clip too; this is the earlier of the two refusals."
        )
    return daemon.RamLoopDeclaration(
        clip_frames=clip_frames,
        total_frames=total_frames,
        # int(round(...)) is safe precisely because the exactness check above
        # passed: the quantity being rounded is already a whole number.
        pts_stride_ns=int(round(clip_frames / plan.fps * 1e9)),
        budget_mb=plan.ram_budget_mb,
        period_ns=0 if not paced_fps else int(round(1e9 / paced_fps)),
    )


# ---------------------------------------------------------------------------
# Running the daemon under measurement
# ---------------------------------------------------------------------------


@dataclass
class Run:
    """One measured run of the daemon. Counters first; verdicts derived."""

    label: str
    proc_width: int
    proc_height: int
    paced_fps: float | None
    wall_s: float = 0.0
    returncode: int = 0
    stream_bytes: int = 0
    stats: dict = field(default_factory=dict)
    measurements: dict[str, metrics.Measurement] = field(default_factory=dict)
    health: dict = field(default_factory=dict)
    stderr_tail: str = ""
    source_mode: str = SOURCE_MODE_INJECT_FILE
    #: The daemon's own ``source_bytes_served`` counter — luma bytes handed to
    #: the detector. Read from the daemon rather than re-derived here as
    #: frames_in * proc_w * proc_h, because the daemon ADOPTS the stream's proc
    #: grid over ``--proc``: a host-side product is the harness's belief about
    #: the grid, the counter is the daemon's statement about what it served.
    source_bytes: int = 0
    ram_plan: dict = field(default_factory=dict)

    # -- derived, all of them honest about what they do not know -------------

    @property
    def frames_in(self) -> int | None:
        """How many frames the daemon consumed, or None if it never said.

        None rather than 0. The daemon writes ``--stats`` exactly once, on the
        way out of its loop, so a killed or crashed run leaves no file at all —
        and a missing stats file read as zero frames turns into "0.00 fps",
        which is a measurement of a run that did not happen. The absence has to
        survive as an absence all the way to the artifact.
        """
        value = self.stats.get("frames_in")
        return None if value is None else int(value)

    @property
    def input_mb_s(self) -> float | None:
        if self.wall_s <= 0.0 or self.stream_bytes <= 0:
            return None
        return self.stream_bytes / self.wall_s / 1e6

    @property
    def source_mb_s(self) -> float | None:
        """What the DETECTOR was fed, from the daemon's own counter.

        Deliberately a different name and a different number from
        :attr:`input_mb_s`, which is what the HOST PUSHED over the link. For a
        looped clip the two differ by the loop factor — 12 frames served 120
        times is a factor of 10, and an hour at 30 fps from a 78-frame clip is
        a factor of 1385 — so one field carrying both would be reporting a
        detector feed as a link rate or the reverse. Both are recorded, each
        labelled, and together they are the answer to finding D8-F8: the link
        carried the clip once, the detector was fed at the full rate.
        """
        if self.wall_s <= 0.0 or self.source_bytes <= 0:
            return None
        return self.source_bytes / self.wall_s / 1e6

    @property
    def source_budget_met(self) -> bool | None:
        """Whether the run served the frame budget it declared.

        None when it declared none — a file or TCP run ends on a trailer, so
        there is no budget to meet and False would be an accusation.
        """
        planned = self.stats.get("source_frames_planned")
        if not planned:
            return None
        return self.stats.get("source_frames_served") == planned

    def source_verdict(self, ceiling_mb_s: float | None) -> str:
        """Whether this run measured the detector or the pipe feeding it.

        With no declared medium there is no denominator and the answer is that
        nobody declared one — not "fine". The threshold is 80% of the medium's
        ceiling: below it the source had headroom, at or above it the number in
        the fps column is at least partly the source's.
        """
        if self.source_mode == SOURCE_MODE_INJECT_RAM:
            # Answered BEFORE any ceiling is consulted, and no denominator is
            # invented. A RAM loop has no source medium at all, so both
            # "SOURCE-BOUND" and "undeclared source medium" would be false
            # statements about it — the second one especially, since it reads
            # as an omission the operator could fix.
            rate = self.source_mb_s
            if rate is None:
                return "RAM-resident: the daemon left no source_bytes_served counter"
            return (
                f"RAM-resident: {rate:.1f} MB/s served from preloaded DDR; no "
                "external source medium in the path"
            )
        rate = self.input_mb_s
        if ceiling_mb_s is None:
            return "undeclared source medium — cannot say"
        if rate is None:
            return "no byte rate recorded"
        if rate >= 0.8 * ceiling_mb_s:
            return (
                f"SOURCE-BOUND: {rate:.1f} MB/s against a declared "
                f"{ceiling_mb_s:.1f} MB/s medium"
            )
        return f"detector-bound: {rate:.1f} MB/s of {ceiling_mb_s:.1f} MB/s available"

    def as_dict(self, ceiling_mb_s: float | None = None) -> dict:
        return {
            "label": self.label,
            "proc_width": self.proc_width,
            "proc_height": self.proc_height,
            "paced_fps": self.paced_fps,
            "wall_s": self.wall_s,
            "returncode": self.returncode,
            "stream_bytes": self.stream_bytes,
            "input_mb_s": self.input_mb_s,
            "source_mode": self.source_mode,
            "source_bytes": self.source_bytes,
            "source_mb_s": self.source_mb_s,
            "source_budget_met": self.source_budget_met,
            "source_verdict": self.source_verdict(ceiling_mb_s),
            "ram_plan": self.ram_plan,
            # On the RUN, not only on the sweep record: this is what makes
            # "every sweep record carries it" literally true and what makes it
            # survive `_run_from_dict`. None on a non-RAM run, because a DDR
            # note on a run with no DDR loop would be a false declaration.
            "ddr_profile_note": (
                DDR_PROFILE_NOTE
                if self.source_mode == SOURCE_MODE_INJECT_RAM
                else None
            ),
            "stats": self.stats,
            "measurements": {
                name: value.as_dict() for name, value in sorted(self.measurements.items())
            },
            "health": self.health,
        }


def _free_port() -> int:
    """A port nothing is using right now.

    The daemon's ``--inject-listen`` takes a fixed port and never reports an
    ephemeral one, so the harness has to choose. This is the usual bind-and-
    release: a small race window remains, and it is preferable to a hard-coded
    port two parallel runs would fight over.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _feed_stream_over_tcp(
    port: int,
    plan: BenchmarkPlan,
    proc_width: int,
    proc_height: int,
    session_uuid: str,
    frame_count: int,
    profile: PtsProfile,
    paced_fps: float | None,
    connect_timeout_s: float = 10.0,
) -> int:
    """Push a generated stream into a listening daemon. Returns bytes sent.

    Frames are generated once and REPLAYED in a loop when more are asked for
    than the scene has. That is what makes an hour-long soak possible without
    an 143 GB file, and it is declared in the artifact: a looped scene means
    the background model meets the same pixels again, which is fine for a
    stability measurement and would be dishonest to hide inside an fps number.
    """
    scene = list(iter_scene_frames(plan, proc_width, proc_height))
    session = benchmark_session(
        plan, proc_width, proc_height, session_uuid, frame_count, profile
    )
    deadline = time.monotonic() + connect_timeout_s
    sock = None
    while sock is None:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    sent = 0
    started = time.monotonic()
    with sock:
        header = encode_session_header(session)
        sock.sendall(header)
        sent += len(header)
        for frame_seq in range(frame_count):
            record = _frame_record(plan, profile, scene[frame_seq % len(scene)], frame_seq)
            if paced_fps:
                due = started + frame_seq / paced_fps
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            sock.sendall(record)
            sent += len(record)
        trailer = encode_trailer(frame_count)
        sock.sendall(trailer)
        sent += len(trailer)
    return sent


#: How often the run loop drains the measurement socket. It bounds how late a
#: process exit can be noticed, and therefore how much of the harness's own
#: latency can land in ``wall_s``; 20 ms against a 33 ms frame period is small
#: enough not to matter and large enough not to spin.
POLL_INTERVAL_S = 0.02

#: How often a run samples thermals. Once every five seconds over an hour is
#: 720 readings, which is a drift curve; once every 20 ms would be 180000
#: readings of a quantity that moves in minutes.
THERMAL_INTERVAL_S = 5.0


class _Feeder(threading.Thread):
    """Push the injection stream on its own thread.

    A soak paces frames for an hour. Doing that on the thread that also drains
    the measurement socket would mean the socket is drained once an hour, and
    the health packets this harness exists to collect would be dropped by the
    kernel long before anyone read them.
    """

    def __init__(
        self,
        port: int,
        plan: BenchmarkPlan,
        proc_width: int,
        proc_height: int,
        session_uuid: str,
        frame_count: int,
        profile: PtsProfile,
        paced_fps: float | None,
    ) -> None:
        super().__init__(name="skyweave-feeder", daemon=True)
        self._args = (port, plan, proc_width, proc_height, session_uuid,
                      frame_count, profile, paced_fps)
        self.sent = 0
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self.sent = _feed_stream_over_tcp(*self._args)
        except OSError as exc:
            # The daemon died before or during the feed. Recorded, not raised
            # into a thread nobody is catching from.
            self.error = exc

    def join(self, timeout: float | None = 30.0) -> None:
        super().join(timeout)


def run_daemon_measured(
    label: str,
    config: DetectorConfig,
    work_dir: str | Path,
    plan: BenchmarkPlan,
    stream_path: Path | None = None,
    tcp_frames: int | None = None,
    ram_clip_path: Path | None = None,
    ram_loop: daemon.RamLoopDeclaration | None = None,
    session_uuid: str = "d8bench-0000-0000-0000-000000000001",
    profile: PtsProfile | None = None,
    build_dir: str | Path | None = None,
    detector: str = "soft",
    health_period_ms: int = 1000,
    paced_fps: float | None = None,
    timeout_s: float = 3600.0,
    thermal_samples: list[metrics.Measurement] | None = None,
    thermal_interval_s: float = THERMAL_INTERVAL_S,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> Run:
    """Run the daemon once, under measurement, and collect everything.

    The measurement sink is a REAL listener rather than the throwaway socket
    ``run_daemon_on_stream`` binds: the health plane rides the measurement port
    (there is no separate one), so the listener has to be there anyway, and one
    bound socket that is actually drained is also what keeps loopback from
    replying port-unreachable and inventing a ``send_failures`` count.

    Two structural details, both of them measurement correctness rather than
    style. The feed runs on its OWN thread, because a paced hour-long soak
    would otherwise block the drain loop for an hour and the health plane it is
    supposed to be watching would arrive into a full socket buffer. And the
    wall time is stamped the moment the process is seen to have exited, not
    after the drain loop's timeout expires: at a 0.2 s poll the harness's own
    granularity was landing inside ``wall_s`` — 0.59 s of measured wall on a
    run the daemon's own clock put at 0.09 s — and every rate derived from it
    inherited the error.
    """
    profile = profile or PtsProfile()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    stats_path = work_dir / "stats.json"

    listener = health.HealthListener(host="127.0.0.1", port=0)
    argv = [str(daemon.daemon_path(build_dir))]
    # Three arms, and the mode is DECLARED in each of them rather than inferred
    # afterwards from whether a stream path happened to be passed. The old
    # inference was ambiguous between the injection modes and was thrown away
    # before it could reach a collected artifact.
    if ram_clip_path is not None:
        if ram_loop is None:
            raise ValueError(
                "a RAM-loop clip without its declaration: a loop has no trailer, "
                "so without a frame budget nothing would end the run"
            )
        argv += ram_loop.as_daemon_args(str(ram_clip_path))
        source_mode = SOURCE_MODE_INJECT_RAM
        inject_port = None
    elif stream_path is not None:
        argv += ["--inject-file", str(stream_path)]
        source_mode = SOURCE_MODE_INJECT_FILE
        inject_port = None
    else:
        inject_port = _free_port()
        argv += ["--inject-listen", str(inject_port)]
        source_mode = SOURCE_MODE_INJECT_TCP
    argv += [
        "--jetson", "127.0.0.1",
        "--measurement-port", str(listener.port),
        "--control-port", "0",
        "--health-period-ms", str(health_period_ms),
        "--stats", str(stats_path),
        "--camera-id", str(plan.camera_id),
    ] + daemon.daemon_args(config, detector=detector)

    run = Run(
        label=label,
        proc_width=config.proc_width,
        proc_height=config.proc_height,
        paced_fps=paced_fps,
        source_mode=source_mode,
    )
    # The daemon's output goes to a FILE, not a pipe. A pipe would be a hang
    # waiting to happen: this loop only drains it after the process exits, so a
    # run that logged more than the pipe buffer — a soak with a warning per
    # frame would do it in seconds — would block forever with both sides
    # waiting for the other.
    log_path = work_dir / "daemon.log"
    # Any stats file from a previous run in this directory goes NOW. The
    # daemon writes it once, on the way out; a run that dies leaves the old
    # one, and this harness reads "if stats_path.exists()" — which would
    # quietly report the previous run's counters as this one's.
    stats_path.unlink(missing_ok=True)
    started = time.monotonic()
    with log_path.open("wb") as log_handle:
        process = subprocess.Popen(argv, stdout=log_handle,
                                   stderr=subprocess.STDOUT)
    sampler = metrics.ProcessSampler(process.pid)
    sampler.start()
    feeder: _Feeder | None = None
    if inject_port is not None:
        feeder = _Feeder(
            port=inject_port, plan=plan, proc_width=config.proc_width,
            proc_height=config.proc_height, session_uuid=session_uuid,
            frame_count=tcp_frames or plan.frames, profile=profile,
            paced_fps=paced_fps,
        )
        feeder.start()
    elif ram_clip_path is not None:
        # The clip crossed the medium exactly ONCE, and its size over the run's
        # wall time is a true — if small — statement about the link, which is
        # the number finding D8-F8 is about. Zeroing it so `input_mb_s` reads
        # None would throw that away; `source_mb_s` carries the other question.
        run.stream_bytes = ram_clip_path.stat().st_size
    else:
        run.stream_bytes = stream_path.stat().st_size

    exited_at = None
    usage = None
    status = 0
    timed_out = False
    next_thermal = 0.0
    try:
        # Drain while it runs so the socket buffer never becomes the thing
        # under test, and sample thermals on a declared, much slower beat.
        #
        # The process is reaped with os.wait4 rather than Popen.wait, because
        # wait4 returns the rusage for THIS PID — the only honest source for
        # the A7-utilisation column (see metrics.cpu_utilisation).
        while True:
            reaped, status, usage = os.wait4(process.pid, os.WNOHANG)
            if reaped == process.pid:
                break
            listener.poll(poll_interval_s)
            now = time.monotonic()
            if thermal_samples is not None and now >= next_thermal:
                thermal_samples.append(metrics.thermal_c())
                next_thermal = now + thermal_interval_s
            if now - started > timeout_s:
                timed_out = True
                process.kill()
                _, status, usage = os.wait4(process.pid, 0)
                break
        exited_at = time.monotonic()
    finally:
        run.wall_s = (exited_at or time.monotonic()) - started
        sampler.stop()
        if feeder is not None:
            feeder.join()
            run.stream_bytes = feeder.sent
        listener.poll(0.5)
        run.health = listener.summary()
        listener.close()
        # Popen must be told the child is gone. Left unset, its __del__ warns,
        # and a later poll() would see ECHILD and helpfully invent a returncode
        # of 0 for a process it never reaped.
        process.returncode = os.waitstatus_to_exitcode(status)

    run.returncode = process.returncode
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
    run.stderr_tail = "\n".join(tail)
    if timed_out:
        run.stderr_tail += f"\nTIMEOUT after {timeout_s} s"
    if feeder is not None and feeder.error is not None:
        run.stderr_tail += f"\nfeed failed: {feeder.error!r}"
    if stats_path.exists():
        run.stats = json.loads(stats_path.read_text(encoding="utf-8"))
    run.source_bytes = int(run.stats.get("source_bytes_served", 0))
    if ram_loop is not None:
        clip_bytes = ram_clip_bytes(
            config.proc_width, config.proc_height, ram_loop.clip_frames
        )
        run.ram_plan = {
            **ram_loop.as_dict(),
            "loop_passes": -(-ram_loop.total_frames // ram_loop.clip_frames),
            "budget_bytes": ram_loop.budget_mb * 1_000_000,
            "detector_state_bytes": detector_state_bytes(
                config.proc_width, config.proc_height, detector
            ),
            "clip_bytes": clip_bytes,
            # The third term of the daemon's own check, carried on the run so a
            # collected artifact shows the same three terms the daemon printed.
            "daemon_fixed_bytes": daemon_fixed_bytes(
                config.proc_width, config.proc_height
            ),
            "basis": (
                "derived arithmetic from the detector's own allocation total, "
                "the clip geometry and a DECLARED upper bound on the daemon's "
                "fixed struct residue; NOT a measurement — peak RSS is the "
                "only measured memory number, and on a board it does not "
                "include the IVE detector's MMZ allocations"
            ),
        }

    daemon_fps = _final_health_fps(listener)
    run.measurements = {
        "sustained_fps_wall": metrics.sustained_fps(run.frames_in, run.wall_s),
        "sustained_fps_daemon": daemon_fps,
        "peak_rss_mb": sampler.peak_rss(),
        "cpu_utilisation": metrics.cpu_utilisation(usage, run.wall_s),
        "ddr_bandwidth_mb_s": metrics.ddr_bandwidth_mb_s(),
        "thermal_c": metrics.thermal_c(),
    }
    for measurement in run.measurements.values():
        measurement.check()
    return run


def _final_health_fps(listener: health.HealthListener) -> metrics.Measurement:
    """The daemon's own view of its rate, off the last health packet.

    Different from the harness's wall-clock figure by exactly the daemon's
    startup: its clock starts after the sockets are open. Both are recorded
    because the difference is the harness's overhead, and a benchmark that
    reports one number cannot show you that.
    """
    if not listener.readings:
        return metrics.not_measured(
            "sustained_fps_daemon", "fps",
            "no health packet arrived; the daemon reports its own rate there",
        )
    return metrics.Measurement(
        name="sustained_fps_daemon",
        value=listener.readings[-1].health.fps,
        unit="fps",
        source="the daemon's last 1 Hz health packet",
    )


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _scene_wrapped(ram_plan: dict) -> bool:
    """Whether a RAM-loop run actually came back round to frame 0.

    A fact about the run, not a property of the source mode. A 30-frame run off
    a 60-frame resident clip never repeats a slot, and a record that asserted
    it had — which both records did, the soak with a literal ``True`` — also
    attached :data:`RAM_LOOP_SCENE_NOTE`, a DECLARED SYSTEMATIC whose whole
    text describes an event that did not occur.
    """
    clip = ram_plan.get("clip_frames")
    served = ram_plan.get("total_frames")
    return bool(clip and served and served > clip)


@dataclass
class SweepResult:
    plan: BenchmarkPlan
    runs: list[Run]
    detector: str
    host: str
    resolutions: tuple[tuple[int, int], ...] = D4_RESOLUTIONS

    def as_dict(self) -> dict:
        ceiling = self.plan.resolved_ceiling_mb_s()
        # The RUN's mode, not the plan's: the plan is a request and the run is
        # what the argv actually said. They differ whenever a plan names a mode
        # the runner does not build (a sweep never listens on TCP), and a
        # record that reported the request would be reporting the wrong source.
        source_mode = self.runs[0].source_mode if self.runs else self.plan.source_mode
        looped = source_mode == SOURCE_MODE_INJECT_RAM
        # `all`, and `bool(self.runs)` so an empty sweep is not a vacuous True:
        # one record cannot say two things, so it says the weaker one, and the
        # per-run clip and served counts are in every `ram_plan` for a reader
        # who needs the row-by-row answer.
        wrapped = looped and bool(self.runs) and all(
            _scene_wrapped(run.ram_plan) for run in self.runs
        )
        return {
            "schema": "d8-benchmark/1",
            "kind": "sweep",
            "plan": self.plan.as_dict(),
            "detector": self.detector,
            "host": self.host,
            "cap_note": CAP_HEADROOM_NOTE,
            # Following cap_note exactly: a declared systematic travels with the
            # record it applies to, and is None where it does not apply rather
            # than present-and-irrelevant.
            "source_mode": source_mode,
            "ddr_profile_note": DDR_PROFILE_NOTE if looped else None,
            # The scene note describes a WRAP, so it travels with the runs that
            # wrapped and is None otherwise — the rule cap_note's comment above
            # already states, applied to the condition rather than to the mode.
            "ram_loop_note": RAM_LOOP_SCENE_NOTE if wrapped else None,
            # Keyed by what RAN, the way SoakResult already did it. Built from
            # `ram_budget_table` this row said 3381 frames while every run's own
            # ram_plan said 6, and `fits` could not be False for any run that
            # could exist, because the table re-derived the maximum every time.
            "ram_budget": (
                [
                    ram_budget_row(
                        run.proc_width, run.proc_height,
                        run.ram_plan.get("clip_frames"), self.detector,
                        self.plan.ram_budget_mb * 1_000_000, self.plan.fps,
                    )
                    for run in self.runs
                ]
                if looped
                else None
            ),
            "scene_looped": wrapped,
            "required_mb_s_at_30fps": {
                f"{w}x{h}": required_mb_s(w, h, 30.0) for w, h in self.resolutions
            },
            "source_media_mb_s": SOURCE_MEDIA,
            "runs": [run.as_dict(ceiling) for run in self.runs],
        }


def run_sweep(
    work_dir: str | Path,
    plan: BenchmarkPlan | None = None,
    resolutions: tuple[tuple[int, int], ...] = D4_RESOLUTIONS,
    build_dir: str | Path | None = None,
    detector: str = "soft",
    keep_streams: bool = False,
) -> SweepResult:
    """One unpaced run per resolution. Frames from a file, ceiling measured."""
    plan = plan or BenchmarkPlan()
    work_dir = Path(work_dir)
    runs = []
    for width, height in resolutions:
        label = f"{width}x{height}"
        row_dir = work_dir / label
        row_dir.mkdir(parents=True, exist_ok=True)
        stream_path = row_dir / "bench.swij"
        session_uuid = f"d8bench-{width}x{height}"[:36]
        ram_loop = None
        if plan.source_mode == SOURCE_MODE_INJECT_RAM:
            ram_loop = ram_loop_declaration(
                plan, width, height, total_frames=plan.frames, detector=detector
            )
        write_benchmark_stream(
            stream_path, plan, width, height,
            session_uuid=session_uuid,
            frame_count=None if ram_loop is None else ram_loop.clip_frames,
        )
        config = benchmark_config(width, height, plan)
        run = run_daemon_measured(
            label=label,
            config=config,
            work_dir=row_dir,
            plan=plan,
            stream_path=None if ram_loop is not None else stream_path,
            ram_clip_path=stream_path if ram_loop is not None else None,
            ram_loop=ram_loop,
            session_uuid=session_uuid,
            build_dir=build_dir,
            detector=detector,
        )
        runs.append(run)
        if not keep_streams:
            # A 2304x1296 row is 358 MB of stream. Deleting it after the run is
            # not tidiness: three rows kept would fill a node's card.
            stream_path.unlink(missing_ok=True)
    return SweepResult(
        plan=plan, runs=runs, detector=detector, host=metrics.host_description(),
        resolutions=resolutions,
    )


# ---------------------------------------------------------------------------
# The soak
# ---------------------------------------------------------------------------


@dataclass
class SoakResult:
    plan: BenchmarkPlan
    run: Run
    duration_s: float
    target_fps: float
    frames_planned: int
    thermal_first: metrics.Measurement
    thermal_last: metrics.Measurement
    detector: str
    host: str

    @property
    def thermal_drift_c(self) -> metrics.Measurement:
        if not (self.thermal_first.measured and self.thermal_last.measured):
            return metrics.not_measured(
                "thermal_drift_c", "C",
                "thermals were NOT-MEASURED on this host, so a drift between "
                "two absences is not a number",
            )
        return metrics.Measurement(
            name="thermal_drift_c",
            value=self.thermal_last.value - self.thermal_first.value,
            unit="C",
            source=f"{self.thermal_last.source}, last minus first",
        )

    def as_dict(self) -> dict:
        # The RUN's mode for the same reason SweepResult uses it: a soak with a
        # default plan is fed over TCP whatever the plan asked for.
        source_mode = self.run.source_mode
        looped_from_ram = source_mode == SOURCE_MODE_INJECT_RAM
        # DERIVED, not asserted. This field was a literal `True` with the
        # comment "a soak loops its scene whichever source carries it" — false
        # for any soak short enough not to reach the end of its scene, which a
        # 2 s soak against the default 120-frame plan is. The RAM arm asks the
        # clip, the TCP arm asks the scene, and neither asks the source mode.
        if looped_from_ram:
            wrapped = _scene_wrapped(self.run.ram_plan)
        else:
            wrapped = self.frames_planned > self.plan.frames
        return {
            "schema": "d8-benchmark/1",
            "kind": "soak",
            "plan": self.plan.as_dict(),
            "detector": self.detector,
            "host": self.host,
            "duration_s": self.duration_s,
            "target_fps": self.target_fps,
            "frames_planned": self.frames_planned,
            # The soak is a scored artifact too, and it was the asymmetric one:
            # the sweep carried cap_note and the soak carried nothing.
            "source_mode": source_mode,
            "ddr_profile_note": DDR_PROFILE_NOTE if looped_from_ram else None,
            # Both conditions: the note's own text is about a RAM clip wrapping,
            # so a wrapping TCP scene is not what it describes either.
            "ram_loop_note": (
                RAM_LOOP_SCENE_NOTE if (looped_from_ram and wrapped) else None
            ),
            "ram_budget": (
                [
                    ram_budget_row(
                        self.run.proc_width, self.run.proc_height,
                        self.run.ram_plan.get("clip_frames"), self.detector,
                        self.plan.ram_budget_mb * 1_000_000, self.plan.fps,
                    )
                ]
                if looped_from_ram
                else None
            ),
            "scene_looped": wrapped,
            "thermal_first": self.thermal_first.as_dict(),
            "thermal_last": self.thermal_last.as_dict(),
            "thermal_drift_c": self.thermal_drift_c.as_dict(),
            "run": self.run.as_dict(self.plan.resolved_ceiling_mb_s()),
        }


def run_soak(
    work_dir: str | Path,
    proc_width: int,
    proc_height: int,
    duration_s: float,
    plan: BenchmarkPlan | None = None,
    target_fps: float = 30.0,
    build_dir: str | Path | None = None,
    detector: str = "soft",
    health_period_ms: int = 1000,
) -> SoakResult:
    """Hold the operating point for a declared interval and watch it.

    A soak is not a benchmark: the sweep asks how fast the node CAN go, the
    soak asks whether it stays there. So this one is PACED at ``target_fps``,
    fed over TCP from a looped scene (an hour of unlooped frames at 1536x864
    would be 143 GB), and the numbers to read afterwards are drops, thermal
    drift and whether the achieved rate held.
    """
    plan = plan or BenchmarkPlan()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    frames_planned = max(1, int(round(duration_s * target_fps)))
    config = benchmark_config(proc_width, proc_height, plan)
    session_uuid = f"d8soak-{proc_width}x{proc_height}"[:36]
    ram_clip_path = None
    ram_loop = None
    if plan.source_mode == SOURCE_MODE_INJECT_RAM:
        # paced_fps flows into the DECLARATION, so the pace becomes an integer
        # nanosecond period the daemon carries out. The RAM arm builds no
        # _Feeder, which is what keeps the run from being paced twice.
        ram_loop = ram_loop_declaration(
            plan, proc_width, proc_height, total_frames=frames_planned,
            detector=detector, paced_fps=target_fps,
        )
        ram_clip_path = work_dir / "soak.swij"
        write_benchmark_stream(
            ram_clip_path, plan, proc_width, proc_height,
            session_uuid=session_uuid, frame_count=ram_loop.clip_frames,
        )
    thermal_samples: list[metrics.Measurement] = []
    first = metrics.thermal_c()
    run = run_daemon_measured(
        label=f"soak-{proc_width}x{proc_height}",
        config=config,
        work_dir=work_dir,
        plan=plan,
        tcp_frames=None if ram_loop is not None else frames_planned,
        ram_clip_path=ram_clip_path,
        ram_loop=ram_loop,
        session_uuid=session_uuid,
        build_dir=build_dir,
        detector=detector,
        health_period_ms=health_period_ms,
        paced_fps=target_fps,
        timeout_s=max(60.0, duration_s * 3.0),
        thermal_samples=thermal_samples,
    )
    last = thermal_samples[-1] if thermal_samples else metrics.thermal_c()
    return SoakResult(
        plan=plan, run=run, duration_s=duration_s, target_fps=target_fps,
        frames_planned=frames_planned, thermal_first=first, thermal_last=last,
        detector=detector, host=metrics.host_description(),
    )


# ---------------------------------------------------------------------------
# E8: two runs, same config, within the DECLARED bounds
# ---------------------------------------------------------------------------

#: Counters a deterministic replay must reproduce EXACTLY. These are not
#: within-a-bound quantities: the same frames through the same detector produce
#: the same components, so a difference here is a defect and no tolerance can
#: be the right answer to it.
EXACT_COUNTERS = (
    "frames_in",
    "frames_scored",
    "capture_events",
    "observations_sent",
    "components_offered",
    "components_emitted",
    "components_dropped_over_cap",
    "frames_at_cap",
    "events_unencodable",
    "components_shed_by_detector",
)


@dataclass
class Reproducibility:
    """What two runs of the same config did and did not agree about."""

    label: str
    #: The OTHER side's label. ``Reproducibility(label=first.label)`` kept one,
    #: so a published verdict could not say that two different configurations
    #: had been compared — the artifact recorded the left-hand name and the
    #: right-hand run vanished into it.
    second_label: str = ""
    exact_mismatches: list[str] = field(default_factory=list)
    relative: dict[str, float] = field(default_factory=dict)
    breaches: list[str] = field(default_factory=list)
    uncompared: list[str] = field(default_factory=list)
    #: Ways the two runs were not the same experiment: a different grid, a
    #: different pace, a different clip, or a bounded axis read by a different
    #: mechanism. E8's premise is "two runs, SAME config"; the comparator
    #: checked exactly one axis of that premise (``source_mode``) and was
    #: otherwise willing to certify a 288x162 run against a 2304x1296 one.
    config_mismatches: list[str] = field(default_factory=list)
    exact_compared: int = 0

    @property
    def compared_anything(self) -> bool:
        return bool(self.relative) or self.exact_compared > 0

    @property
    def verdict(self) -> str:
        """``pass``, ``fail`` or ``incomplete`` — three states, not two.

        The third one is the whole point. Two runs in which an axis was
        NOT-MEASURED on both sides produce no mismatch and no breach, so a
        two-valued verdict calls that agreement. It is not agreement; it is an
        empty comparison wearing a verdict, and the board scenario that
        produces it is ordinary: no health packet arrives (finding D8-F9), so
        the only bounded fps axis is absent, and a daemon that died leaves no
        stats file, so every counter is absent too. E8 would then report
        "within the declared bounds" for a pair of runs where nothing was ever
        compared to anything.

        ``fail`` outranks ``incomplete``: a run that breached a bound AND lost
        an axis has still breached a bound.

        A configuration mismatch is ``fail`` and not ``incomplete``, for the
        same reason a differing ``source_mode`` always was: two configurations
        are two experiments, and E8's whole claim is about one.
        """
        if self.exact_mismatches or self.breaches or self.config_mismatches:
            return "fail"
        if self.uncompared or not self.compared_anything:
            return "incomplete"
        return "pass"

    @property
    def passes(self) -> bool:
        return self.verdict == "pass"

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            # Both sides, so a published verdict can be audited months later
            # against which two runs it actually compared.
            "labels": [self.label, self.second_label or self.label],
            "passes": self.passes,
            "verdict": self.verdict,
            "compared_anything": self.compared_anything,
            "exact_compared": self.exact_compared,
            "exact_mismatches": self.exact_mismatches,
            "config_mismatches": self.config_mismatches,
            "relative": self.relative,
            "breaches": self.breaches,
            "uncompared": self.uncompared,
        }


def _relative_difference(a: float, b: float) -> float:
    mean = (abs(a) + abs(b)) / 2.0
    if mean == 0.0:
        return 0.0
    return abs(a - b) / mean


def compare_runs(
    first: Run, second: Run, bounds: BenchmarkTolerance = BENCHMARK_RUN_TO_RUN
) -> Reproducibility:
    """E8's comparison: exact where the system is deterministic, bounded where
    it is not, and NOT COMPARED — said out loud — where a number is missing.

    The third case is the one worth guarding. If DDR bandwidth is NOT-MEASURED
    in both runs, the axes agree in the sense that neither has a number, and a
    comparator that returned "pass" would be reporting agreement about nothing.
    Such axes land in ``uncompared`` and never in ``passes``.

    And BEFORE any of that, the premise: "two runs, same config". It was never
    checked past ``source_mode``, so a 288x162 run compared against a 2304x1296
    one — different grid, different clip, peak RSS read by procfs on one side
    and by sampled ``ps`` on the other — certified ``pass`` with an empty
    ``uncompared``. Configuration axes go in ``config_mismatches``, which forces
    ``fail`` and is deliberately outside ``exact_compared``, whose pin to
    ``len(EXACT_COUNTERS)`` several tests rely on. What this function can see is
    what a ``Run`` carries; the sweep-level plan, detector and host are checked
    one level up by :func:`compare_records`.
    """
    result = Reproducibility(label=first.label, second_label=second.label)
    if first.label != second.label:
        result.config_mismatches.append(
            f"label: {first.label} != {second.label}"
        )
    if (first.proc_width, first.proc_height) != (second.proc_width, second.proc_height):
        result.config_mismatches.append(
            f"proc grid: {first.proc_width}x{first.proc_height} != "
            f"{second.proc_width}x{second.proc_height}"
        )
    if first.paced_fps != second.paced_fps:
        result.config_mismatches.append(
            f"paced_fps: {first.paced_fps} != {second.paced_fps}"
        )
    if first.ram_plan != second.ram_plan:
        result.config_mismatches.append(
            f"ram_plan: {first.ram_plan} != {second.ram_plan}"
        )
    # A RAM loop ends on a DECLARED frame budget. A run an operator SIGTERMed
    # instead served fewer frames, and every one of EXACT_COUNTERS is monotone
    # in frames served — so comparing it would report ten legitimate exact
    # mismatches, i.e. a determinism defect that is not one. Said up front, and
    # the verdict becomes `incomplete` rather than `fail`.
    for run in (first, second):
        planned = run.stats.get("source_frames_planned")
        served = run.stats.get("source_frames_served")
        if planned and served != planned:
            result.uncompared.append(
                f"{run.label}: served {served} of a declared {planned} frames, so "
                "the run did not serve its declared frame budget (stopped by a "
                "signal) and its deterministic counters are a sample of wall time"
            )
    left_mode = first.stats.get("source_mode")
    right_mode = second.stats.get("source_mode")
    if left_mode and right_mode and left_mode != right_mode:
        # A mismatch, not an uncompared axis: two sources are two experiments.
        # Deliberately NOT counted in exact_compared, which is pinned equal to
        # len(EXACT_COUNTERS).
        result.exact_mismatches.append(f"source_mode: {left_mode} != {right_mode}")
    for key in EXACT_COUNTERS:
        left = first.stats.get(key)
        right = second.stats.get(key)
        if left is None or right is None:
            result.uncompared.append(f"{key}: absent from one of the two stats files")
            continue
        result.exact_compared += 1
        if left != right:
            result.exact_mismatches.append(f"{key}: {left} != {right}")

    axes = (
        ("sustained_fps_daemon", bounds.fps_relative),
        ("peak_rss_mb", bounds.peak_rss_relative),
        ("cpu_utilisation", bounds.cpu_utilisation_relative),
    )
    for name, bound in axes:
        left = first.measurements.get(name)
        right = second.measurements.get(name)
        if left is None or right is None or not (left.measured and right.measured):
            reason = "not collected"
            if left is not None and not left.measured:
                reason = left.reason
            elif right is not None and not right.measured:
                reason = right.reason
            result.uncompared.append(f"{name}: {reason}")
            continue
        if left.source != right.source:
            # `metrics`' own module docstring: peak RSS from procfs VmHWM and
            # peak RSS sampled with `ps` "are different claim[s] about the same
            # quantity ... conflating them is not [legitimate], so
            # Measurement.source is mandatory". The comparator that consumes
            # those measurements dropped `source` on the floor, so a bound could
            # be certified across two mechanisms.
            result.config_mismatches.append(
                f"{name}: read by different mechanisms "
                f"({left.source!r} vs {right.source!r})"
            )
        delta = _relative_difference(left.value, right.value)
        result.relative[name] = delta
        if delta > bound:
            result.breaches.append(
                f"{name}: {delta:.4f} relative > declared {bound:.4f} "
                f"({left.value:.4f} vs {right.value:.4f})"
            )
    return result


#: Record-level configuration a pair of E8 runs has to share. The plan block is
#: compared key by key so a report names the field that moved rather than
#: printing two dicts; ``detector`` and ``host`` are beside it because "the
#: same configuration" on a different backend or a different machine is two
#: experiments in the two most consequential ways available.
_RECORD_CONFIG_KEYS = ("detector", "host")


def compare_records(first: dict, second: dict) -> dict:
    """E8 over two collected sweep records, premise first.

    ``compare_runs`` can only see what a ``Run`` carries, and seed, frames,
    warm-up, movers, cap, detector and host are sweep-level keys. The CLI used
    to zip the two run lists by index and cross-check none of them, so two
    sweeps at different SEEDS — genuinely different scenes, byte-for-byte
    different streams — certified ``passes: true`` at every D4 resolution.

    Record-level mismatches are reported once, at the top, and force
    ``passes`` false. They are NOT copied into each verdict: a per-run verdict
    is about that run, and one plan-level difference is one fact.
    """
    config_mismatches: list[str] = []
    left_plan = first.get("plan") or {}
    right_plan = second.get("plan") or {}
    for key in sorted(set(left_plan) | set(right_plan)):
        if left_plan.get(key) != right_plan.get(key):
            config_mismatches.append(
                f"plan.{key}: {left_plan.get(key)!r} != {right_plan.get(key)!r}"
            )
    for key in _RECORD_CONFIG_KEYS:
        if first.get(key) != second.get(key):
            config_mismatches.append(f"{key}: {first.get(key)!r} != {second.get(key)!r}")
    verdicts = [
        compare_runs(_run_from_dict(a), _run_from_dict(b)).as_dict()
        for a, b in zip(first["runs"], second["runs"], strict=True)
    ]
    # `verdicts and ...` on purpose: `all([])` is True, and an empty run list
    # passing would be the same empty-comparison lie one level up.
    return {
        "schema": "d8-benchmark-compare/1",
        "config_mismatches": config_mismatches,
        # Both sides' identity, so a verdict read off a collected artifact says
        # WHICH two records produced it.
        "records": [
            {"detector": record.get("detector"), "host": record.get("host"),
             "seed": (record.get("plan") or {}).get("seed"),
             "source_mode": record.get("source_mode")}
            for record in (first, second)
        ],
        "verdicts": verdicts,
        "passes": (
            bool(verdicts)
            and not config_mismatches
            and all(v["passes"] for v in verdicts)
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="D8.1 board benchmark runner")
    sub = parser.add_subparsers(dest="command", required=True)

    sweep = sub.add_parser("sweep", help="one unpaced run per D4 resolution")
    sweep.add_argument("--work", required=True, help="scratch directory (large)")
    sweep.add_argument("--out", required=True, help="where the JSON result lands")
    sweep.add_argument("--frames", type=int, default=120)
    sweep.add_argument("--seed", type=int, default=SCENE_SEED)
    sweep.add_argument("--detector", choices=("soft", "ive"), default="soft")
    sweep.add_argument("--build-dir", default=None)
    sweep.add_argument(
        "--source-medium", choices=sorted(SOURCE_MEDIA), default=None,
        help="what fed this run; without it no source-bound verdict is possible",
    )
    sweep.add_argument(
        "--source-mode", choices=SOURCE_MODES, default=SOURCE_MODE_INJECT_FILE,
        help="which source the daemon reads; inject-ram preloads a clip and loops it",
    )
    sweep.add_argument(
        "--ram-clip-frames", type=int, default=None,
        help="clip length for --source-mode inject-ram; omitted, it is derived "
             "from the budget and refused if it does not fit",
    )
    sweep.add_argument("--ram-budget-mb", type=int, default=160)

    soak = sub.add_parser("soak", help="hold a paced rate for a declared interval")
    soak.add_argument("--work", required=True)
    soak.add_argument("--out", required=True)
    soak.add_argument("--proc", required=True, help="WxH")
    soak.add_argument("--duration-s", type=float, default=3600.0)
    soak.add_argument("--target-fps", type=float, default=30.0)
    soak.add_argument("--frames", type=int, default=120, help="scene length before it loops")
    soak.add_argument("--seed", type=int, default=SCENE_SEED)
    soak.add_argument("--detector", choices=("soft", "ive"), default="soft")
    soak.add_argument("--build-dir", default=None)
    # The soak had no --source-medium at all, which was a pre-existing gap and
    # not a decision: a soak record with no declared medium cannot say whether
    # it held its rate or the pipe did.
    soak.add_argument(
        "--source-medium", choices=sorted(SOURCE_MEDIA), default=None,
        help="what fed this run; without it no source-bound verdict is possible",
    )
    soak.add_argument(
        "--source-mode", choices=SOURCE_MODES, default=SOURCE_MODE_INJECT_TCP,
        help="which source the daemon reads; inject-ram preloads a clip and loops it",
    )
    soak.add_argument("--ram-clip-frames", type=int, default=None)
    soak.add_argument("--ram-budget-mb", type=int, default=160)

    compare = sub.add_parser("compare", help="E8: two sweep results, same config")
    compare.add_argument("first")
    compare.add_argument("second")
    compare.add_argument("--out", default=None)

    args = parser.parse_args(argv)

    if args.command == "sweep":
        plan = BenchmarkPlan(frames=args.frames, seed=args.seed,
                             source_medium=args.source_medium,
                             source_mode=args.source_mode,
                             ram_clip_frames=args.ram_clip_frames,
                             ram_budget_mb=args.ram_budget_mb)
        result = run_sweep(args.work, plan=plan, build_dir=args.build_dir,
                           detector=args.detector)
        Path(args.out).write_text(
            json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.out}")
        return

    if args.command == "soak":
        width, _, height = args.proc.partition("x")
        plan = BenchmarkPlan(frames=args.frames, seed=args.seed,
                             source_medium=args.source_medium,
                             source_mode=args.source_mode,
                             ram_clip_frames=args.ram_clip_frames,
                             ram_budget_mb=args.ram_budget_mb)
        result = run_soak(
            args.work, int(width), int(height), duration_s=args.duration_s,
            plan=plan, target_fps=args.target_fps, build_dir=args.build_dir,
            detector=args.detector,
        )
        Path(args.out).write_text(
            json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.out}")
        return

    first = json.loads(Path(args.first).read_text(encoding="utf-8"))
    second = json.loads(Path(args.second).read_text(encoding="utf-8"))
    payload = compare_records(first, second)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)


def _run_from_dict(payload: dict) -> Run:
    """Rebuild enough of a Run to compare two recorded sweeps.

    Only the fields the comparison reads. A partial reconstruction is stated
    here rather than pretended away: `compare` works on artifacts, and an
    artifact does not carry a live process.
    """
    run = Run(
        label=payload["label"],
        proc_width=payload["proc_width"],
        proc_height=payload["proc_height"],
        paced_fps=payload.get("paced_fps"),
        wall_s=payload.get("wall_s", 0.0),
        stats=payload.get("stats", {}),
        source_mode=payload.get("source_mode", SOURCE_MODE_INJECT_FILE),
        source_bytes=payload.get("source_bytes", 0) or 0,
        ram_plan=payload.get("ram_plan") or {},
    )
    for name, item in (payload.get("measurements") or {}).items():
        value = item.get("value")
        run.measurements[name] = metrics.Measurement(
            name=name,
            value=None if value == metrics.NOT_MEASURED else float(value),
            unit=item.get("unit", ""),
            source=item.get("source", ""),
            reason=item.get("reason", ""),
        )
    return run


if __name__ == "__main__":
    main()


__all__ = [
    "BenchmarkPlan",
    "D4_RESOLUTIONS",
    "DAEMON_STRUCT_ALLOWANCE_BYTES",
    "DDR_PROFILE_NOTE",
    "EXACT_COUNTERS",
    "FULL_HEIGHT",
    "FULL_WIDTH",
    "RAM_LOOP_BUDGET_BYTES",
    "RAM_LOOP_SCENE_NOTE",
    "Reproducibility",
    "Run",
    "SOURCE_MEDIA",
    "SOURCE_MODES",
    "SOURCE_MODE_CAPTURE_VI",
    "SOURCE_MODE_INJECT_FILE",
    "SOURCE_MODE_INJECT_RAM",
    "SOURCE_MODE_INJECT_TCP",
    "SoakResult",
    "SweepResult",
    "benchmark_config",
    "benchmark_session",
    "compare_records",
    "compare_runs",
    "daemon_fixed_bytes",
    "detector_state_bytes",
    "iter_scene_frames",
    "ram_budget_row",
    "ram_budget_table",
    "ram_clip_bytes",
    "ram_loop_declaration",
    "ram_loop_max_frames",
    "required_mb_s",
    "run_daemon_measured",
    "run_sweep",
    "run_soak",
    "write_benchmark_stream",
]
