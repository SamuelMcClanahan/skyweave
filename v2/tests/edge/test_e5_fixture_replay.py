"""E5: fixture replay through the C daemon, on the host.

The brief allows E5 to run host-side "where feasible; otherwise E5 runs in
D8.2 on the board". It is feasible: the daemon compiles for the host with a
portable GMM2 standing in for the hardware IVE block, so the whole chain —
injection stream, detector, persistence, cap, nanopb, framing — runs here.

What that does and does not prove is the point of this file:

- it DOES prove the pipeline, the cap, the drop counters and the wire path,
  because those are the same code on the board;
- it does NOT prove the hardware GMM2, which is a different detector and is
  measured in D8.2 against the separately DECLARED
  ``BOARD_IVE_TOLERANCE``.

The gate is the declared tolerance (``HOST_SOFT_TOLERANCE``), fixed in
``skyweave2/edge/tolerance.py`` and quoted in ``D8_EDGE_REPORT.md`` before
any of these numbers existed. A second test asserts the stronger thing that
happens to be true today — the portable detector is byte-exact against the
oracle — and says plainly what to do if it ever stops being true.
"""

from __future__ import annotations

import pytest

from skyweave2.edge import daemon, tolerance
from skyweave2.edge.injection import PtsProfile, build_injection_stream
from skyweave2.edge.tolerance import HOST_SOFT_TOLERANCE, compare_to_oracle
from skyweave2.transport import codec
from skyweave2.transport.wire import DATAGRAM_CEILING_BYTES, unframe
from tests.edge.conftest import (
    REPLAYABLE_FIXTURES,
    load_config,
    load_events,
    load_observations,
    load_packets,
    load_stats,
)


@pytest.fixture(scope="module")
def replays(scene_clips, edge_build_dir, tmp_path_factory):
    """Replay every regenerable fixture through the daemon, once."""
    root = tmp_path_factory.mktemp("e5-replay")
    out = {}
    for name in REPLAYABLE_FIXTURES:
        config = load_config(name)
        session = load_stats(name)["session_uuid"]
        stream = root / f"{name}.swij"
        stream.write_bytes(
            build_injection_stream(scene_clips[name], config, session,
                                   profile=PtsProfile())
        )
        run = daemon.run_daemon_on_stream(stream, config, root / name,
                                          build_dir=edge_build_dir)
        assert run.returncode == 0, f"{name}: daemon failed\n{run.stderr}"
        observations = []
        for packet in run.packets:
            observations += codec.decode_observation_packet(unframe(packet)[1])[1]
        out[name] = (run, observations)
    return out


# ---------------------------------------------------------------------------
# The declared gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REPLAYABLE_FIXTURES)
def test_the_replay_holds_the_declared_detector_tolerance(replays, name):
    _, observations = replays[name]
    divergence = compare_to_oracle(
        load_observations(name), observations, HOST_SOFT_TOLERANCE
    )
    breaches = divergence.breaches(HOST_SOFT_TOLERANCE)
    assert not breaches, (
        f"{name}: the daemon's detector left the bounds DECLARED before this run\n"
        + "\n".join(f"  - {line}" for line in breaches)
        + f"\n  measured: {divergence.as_dict()}"
    )
    # Non-vacuous: a comparison with nothing in it passes every bound.
    assert divergence.oracle_observations > 0
    assert divergence.matched > 0


def test_the_tolerance_comparison_can_actually_fail(replays):
    """A gate that cannot fail is decoration. Perturb one centroid past the
    declared bound and require the comparison to notice."""
    _, observations = replays["sparse"]
    oracle = load_observations("sparse")
    nudged = [
        obs.model_copy(update={"u": obs.u + 50.0}) if index % 2 == 0 else obs
        for index, obs in enumerate(observations)
    ]
    divergence = compare_to_oracle(oracle, nudged, HOST_SOFT_TOLERANCE)
    assert divergence.breaches(HOST_SOFT_TOLERANCE)


# ---------------------------------------------------------------------------
# The stronger claim that is true today
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REPLAYABLE_FIXTURES)
def test_the_portable_detector_is_currently_byte_exact(replays, name):
    """The daemon's datagrams are byte-identical to the host oracle's.

    This is STRONGER than the declared tolerance above, which is the actual
    contract. It holds because the portable detector is a faithful
    transcription of `ive_approx` — same float32 state, same numpy promotion
    points, same structuring element — and because the build pins
    `-ffp-contract=off` so no compiler fuses a multiply-add the oracle
    rounds twice.

    If this ever fails while the tolerance test above still passes, that is a
    FINDING to record (a compiler, a libm, an architecture), not automatically
    a regression. Investigate and write it down; do not delete this test to
    make the suite green.
    """
    run, _ = replays[name]
    assert run.packets == load_packets(name), (
        f"{name}: the daemon's datagrams differ from the oracle's. Compare against "
        "the declared-tolerance test: if that one passes, this is a precision "
        "finding, not a defect."
    )


# ---------------------------------------------------------------------------
# The saturated branch of the confidence, which no committed fixture reaches
# ---------------------------------------------------------------------------

_SAT_FULL_WIDTH = 640
_SAT_FULL_HEIGHT = 480
_SAT_FRAMES = 30
_SAT_WARMUP = 10

# Blob widths, at FULL resolution, for the two clips below. Chosen to place
# the resulting proc-resolution AREAS where each clip needs them; nothing else
# about the detector is touched (anti-tuning).
#
# STRADDLE: components from 38 px to 91 px, including 49, 50 and 51 — below
# the saturation area, exactly on it, and well above it. Five movers against a
# cap of seven, so the cap stays inert and every component reaches the wire
# where its bytes can be compared.
_STRADDLE_SIGMAS_PX = (6.0, 6.3, 6.6, 6.9, 8.0)
# CROWDED: nine movers, all comfortably saturated, against a cap of seven.
_CROWDED_SIGMAS_PX = (8.0, 8.6, 9.2, 9.8, 10.4, 11.0, 11.6, 12.2, 12.8)


def _fat_clip(out_dir, sigmas, source):
    """Slow movers wide enough to saturate the confidence, on a fixed seed.

    Neither of the clips built from this is a committed fixture. They cover
    BRANCHES the three committed fixtures cannot reach (their busiest
    component is 40 px against a 50 px saturation area), and a fourth and
    fifth committed fixture would put tolerance-table rows in the report that
    nothing declares. Seeded for the same reason every artifact-producing path
    in this project is: a flaky pixel here would surface as a wire-identity
    failure, which is the most expensive thing in the suite to misread.
    """
    import numpy as np

    from skyweave2.eval.clips import write_clip

    v_grid, u_grid = np.meshgrid(
        np.arange(_SAT_FULL_HEIGHT, dtype=np.float64),
        np.arange(_SAT_FULL_WIDTH, dtype=np.float64),
        indexing="ij",
    )
    frames = []
    for seq in range(_SAT_FRAMES):
        rng = np.random.default_rng(np.random.SeedSequence([7, seq, 0x5A7]))
        image = 96.0 + rng.normal(
            0.0, 2.0, size=(_SAT_FULL_HEIGHT, _SAT_FULL_WIDTH)
        )
        if seq >= _SAT_WARMUP:
            for index, sigma in enumerate(sigmas):
                cu = 60.0 + 110.0 * (index % 5) + 2.0 * (seq - _SAT_WARMUP)
                cv = 110.0 + 150.0 * (index // 5)
                image += 90.0 * np.exp(
                    -0.5
                    * (
                        ((u_grid - cu) / sigma) ** 2 + ((v_grid - cv) / sigma) ** 2
                    )
                )
        frames.append(np.clip(np.rint(image), 0, 255).astype("uint8"))
    write_clip(frames, out_dir, fps=30.0, source=source)
    return out_dir


def _saturating_config(revision):
    from skyweave2.detector.config import Backend, DetectorConfig

    # Shipped defaults except geometry and warm-up, exactly as the committed
    # fixtures' configs are (anti-tuning): no threshold is touched to make the
    # components big, only the rendered blobs are.
    return DetectorConfig(
        backend=Backend.IVE_APPROX,
        proc_width=320,
        proc_height=240,
        warmup_frames=_SAT_WARMUP,
        fps=30.0,
        detector_rev=revision,
        calibration_rev="d8-fixture-cal",
    )


def _replay_against_oracle(name, sigmas, edge_build_dir, tmp_path):
    """Build a clip, run the oracle and the daemon over it, return both."""
    from skyweave2.edge import fixtures as edge_fixtures

    clip = _fat_clip(tmp_path / name, sigmas, f"e5 {name} clip")
    config = _saturating_config(f"d8-{name}/1")
    build = edge_fixtures.build_fixture(name, clip, config, seed=None)
    stream = tmp_path / f"{name}.swij"
    stream.write_bytes(
        build_injection_stream(clip, config, build.session_uuid, profile=PtsProfile())
    )
    run = daemon.run_daemon_on_stream(
        stream, config, tmp_path / f"{name}-run", build_dir=edge_build_dir
    )
    assert run.returncode == 0, run.stderr
    return build, run


def test_the_daemon_matches_the_oracle_across_the_saturation_boundary(
    edge_build_dir, tmp_path
):
    """Host/edge byte identity ON the boundary, not merely past it.

    `component_confidence` is `min(1.0, area_px / 50.0)` on the host and an
    integer `area_px >= 50` short-circuit in `sw_pipeline.c`. Below 50 both
    sides run the same double division and the committed replays cover that
    on hundreds of observations. At and above 50 they are different
    expressions, and no committed fixture goes there.

    The first version of this test used one fat mover and produced areas of
    70-90 px. That covers the clamp but pins nothing: the daemon's `50` could
    be changed to any value in 41..69 and the whole suite stayed green,
    because no area in that window was ever fed to it. The adversarial review
    caught it. This clip straddles instead — 38 px to 91 px, including 49, 50
    and 51 — so the constant itself is what the byte gate holds down, and the
    assertions below fail if the clip ever drifts out of that band.

    Exactly 50 px is deliberately included even though it cannot discriminate
    `>` from `>=`: at 50 both expressions produce a bit-identical 1.0. It is
    there so a reader can see the boundary is exercised rather than inferred.
    """
    build, run = _replay_against_oracle(
        "straddle", _STRADDLE_SIGMAS_PX, edge_build_dir, tmp_path
    )
    areas = sorted(obs.area_px for obs in build.observations)
    assert areas, "no observations"
    assert areas[0] < 50 <= areas[-1], (
        f"the clip no longer straddles the saturation point (areas "
        f"{areas[0]}..{areas[-1]}); it has stopped pinning the constant"
    )
    assert {49, 50, 51} <= set(areas), (
        f"the clip no longer lands on the saturation boundary (areas "
        f"{sorted(set(areas))}); an off-by-a-few threshold would go unnoticed"
    )
    assert all(
        obs.confidence == 1.0 for obs in build.observations if obs.area_px >= 50
    )
    assert run.stats["components_dropped_over_cap"] == 0, (
        "the cap bit on the straddle clip, so the components below saturation "
        "are being dropped instead of compared"
    )
    assert run.packets == build.packets, (
        "the daemon and the host oracle disagree on a clip that crosses the "
        "saturation point; below it they agree, so this is the integer "
        "short-circuit in sw_pipeline.c against Python's min()"
    )


def test_the_daemon_matches_the_oracle_when_saturated_components_compete(
    edge_build_dir, tmp_path
):
    """The cap's `-area_px` level, which the amendment left uncovered.

    `rank_key`/`compare_ranked` rank on confidence first and area second.
    Before the amendment confidence was constant, so level 2 decided every
    capped frame and the clutter fixture exercised it. After the amendment
    confidence is injective in area BELOW saturation, so level 1 ties if and
    only if level 2 does — and level 2's comparison stops being the deciding
    one anywhere the suite looks. It becomes decisive again only when several
    SATURATED components compete for the cap, which nothing else here builds:
    clutter exceeds the cap but never saturates, and the straddle clip
    saturates but never exceeds the cap.

    That regime is not exotic. It is what a crowded frame at the deployment
    resolution looks like, and it sits inside the absolute byte gate, so the C
    comparator getting it wrong would be a silent host/edge divergence. Nine
    fat movers against a cap of seven put it under test.
    """
    build, run = _replay_against_oracle(
        "crowded", _CROWDED_SIGMAS_PX, edge_build_dir, tmp_path
    )
    assert all(obs.confidence == 1.0 for obs in build.observations), (
        "some component failed to saturate, so confidence is still doing the "
        "deciding and the area level remains untested"
    )
    assert run.stats["components_dropped_over_cap"] > 0, (
        "the cap never bit, so nothing competed and compare_ranked's second "
        "level was never reached"
    )
    assert run.stats["frames_at_cap"] > 0
    assert run.stats["max_components_offered"] > build.config.max_components_per_frame
    assert run.packets == build.packets, (
        "the daemon and the host oracle kept DIFFERENT saturated components "
        "at the cap; confidence ties at 1.0 for all of them, so this is the "
        "area tie-break in compare_ranked disagreeing with rank_key"
    )


# ---------------------------------------------------------------------------
# Cap, counters and health behaviour through the real daemon
# ---------------------------------------------------------------------------


def test_the_cap_bites_on_the_clutter_replay_and_is_counted(replays):
    run, observations = replays["clutter"]
    config = load_config("clutter")
    stats = run.stats
    assert stats["max_components_offered"] > config.max_components_per_frame
    assert stats["components_dropped_over_cap"] > 0
    assert stats["frames_at_cap"] > 0
    assert stats["components_offered"] == (
        stats["components_emitted"] + stats["components_dropped_over_cap"]
    )
    assert stats["components_emitted"] == len(observations)
    # The counters the host recorded when it built the fixture, and the ones
    # the daemon recorded replaying it, are about the same frames.
    oracle_stats = load_stats("clutter")["detector"]
    assert stats["frames_scored"] == oracle_stats["frames"]
    assert stats["max_components_offered"] == oracle_stats["max_components_offered"]


def test_the_cap_never_bites_on_the_sparse_replay(replays):
    """The fixture the detector tolerance is measured on must keep the cap
    INERT, or a cap-selection difference would be read as a detection error."""
    run, _ = replays["sparse"]
    assert run.stats["components_dropped_over_cap"] == 0
    assert run.stats["frames_at_cap"] == 0
    assert run.stats["max_components_offered"] < load_config(
        "sparse"
    ).max_components_per_frame


def test_no_capture_event_was_unencodable(replays):
    """With the cap in place a crowded frame must never become a lost
    measurement — that is the entire reason the cap exists (D7-F1)."""
    for name in REPLAYABLE_FIXTURES:
        run, _ = replays[name]
        assert run.stats["events_unencodable"] == 0, name
        assert run.stats["frames_detector_failed"] == 0, name


def test_every_datagram_the_daemon_sent_fits_the_ceiling(replays):
    for name in REPLAYABLE_FIXTURES:
        run, _ = replays[name]
        assert run.packets
        assert max(len(packet) for packet in run.packets) <= DATAGRAM_CEILING_BYTES


def test_the_health_drop_total_includes_the_cap_drops(replays):
    """The brief: dropped components are counted in the health path, never
    silent. `drops` is a total by design (HealthPacket has one counter and
    this phase may not add another); what must never happen is a node that
    discarded measurements and reported zero."""
    run, _ = replays["clutter"]
    assert run.stats["health_drops_total"] >= run.stats["components_dropped_over_cap"]
    assert run.stats["health_drops_total"] > 0
    assert run.stats["health_sent"] > 0

    sparse_run, _ = replays["sparse"]
    # ...and a node that dropped nothing must not invent a drop.
    assert sparse_run.stats["health_drops_total"] == 0


def test_the_daemons_health_packets_decode_on_the_unchanged_host_stack(
    scene_clips, edge_build_dir, tmp_path
):
    """A host-side precursor to the board-gated E7.

    The datagrams are taken off a real socket here rather than from the
    packet log, because the health plane has no log — and because "the
    unchanged v2 stack decodes them" is the claim, so the host's own
    ``codec.decode_health`` has to be the thing that reads them.
    """
    import socket
    import subprocess

    from skyweave2.contracts import ClockDomain
    from skyweave2.edge.injection import build_injection_stream
    from skyweave2.transport.wire import PayloadType

    config = load_config("clutter")
    stream = tmp_path / "clutter.swij"
    stream.write_bytes(
        build_injection_stream(scene_clips["clutter"], config, "e5-health-000000000000")
    )
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sink.settimeout(5.0)
    try:
        argv = [
            str(daemon.daemon_path(edge_build_dir)),
            "--inject-file", str(stream),
            "--measurement-port", str(sink.getsockname()[1]),
            "--control-port", "0",
            # Faster than 1 Hz so a short replay still produces several. The
            # CADENCE itself is E7's business and needs a board running in
            # real time; what this test gates is the CONTENT.
            "--health-period-ms", "50",
        ] + daemon.daemon_args(config)
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=300,
                                   check=False)
        assert completed.returncode == 0, completed.stderr

        observations = 0
        health = []
        try:
            while True:
                payload_type, body = unframe(sink.recv(65535))
                if payload_type is PayloadType.OBSERVATION:
                    observations += 1
                elif payload_type is PayloadType.HEALTH:
                    health.append(codec.decode_health(body))
        except TimeoutError:
            pass
    finally:
        sink.close()

    assert observations > 0, "no measurement datagrams arrived"
    assert health, "no health datagrams arrived"
    last = health[-1]
    assert last.session_uuid == "e5-health-000000000000"
    # The node's OWN clock, not the measurement envelope's declared domain:
    # health is telemetry about the node, timed by the node.
    assert last.clock_domain is ClockDomain.NODE_MONO
    # The cap drops reached the wire. This is the brief's "never silent".
    assert last.drops > 0
    assert last.imu_quaternion == (0.0, 0.0, 0.0, 1.0), (
        "no IMU is wired yet, so the identity rotation must be sent EXPLICITLY "
        "rather than the field omitted"
    )
    assert last.fps > 0.0


def test_the_daemon_sent_what_it_logged(replays):
    """The packet log is the evidence this whole file rests on, so it is
    cross-checked against the socket counter rather than trusted."""
    for name in REPLAYABLE_FIXTURES:
        run, _ = replays[name]
        assert run.stats["datagrams_sent"] == len(run.packets), name
        assert run.stats["send_failures"] == 0, name
        assert run.stats["bytes_sent"] == sum(len(p) for p in run.packets), name


def test_the_replayed_stream_regroups_into_the_same_capture_events(replays):
    """One capture event per datagram, never split: the daemon's datagram
    boundaries must be the oracle's event boundaries."""
    for name in REPLAYABLE_FIXTURES:
        run, _ = replays[name]
        oracle_events = load_events(name)
        assert len(run.packets) == len(oracle_events), name
        for packet, (envelope, observations) in zip(
            run.packets, oracle_events, strict=True
        ):
            decoded_envelope, decoded = codec.decode_observation_packet(
                unframe(packet)[1]
            )
            assert decoded_envelope.frame_seq == envelope.frame_seq, name
            assert len(decoded) == len(observations), name


# ---------------------------------------------------------------------------
# The board tolerance exists and is declared, even though nothing measures it
# ---------------------------------------------------------------------------


def test_the_board_tolerance_is_declared_before_any_board_exists():
    """The anti-tuning rule made checkable: the IVE bounds must be present,
    wider than the host bounds on every axis (four structural divergences are
    known in advance), and must not have been quietly relaxed to whatever the
    first board run produced — because there has not been one."""
    board = tolerance.BOARD_IVE_TOLERANCE
    host = tolerance.HOST_SOFT_TOLERANCE
    assert tolerance.DECLARED_TOLERANCE["ive"] is board
    assert tolerance.DECLARED_TOLERANCE["soft"] is host
    for field in (
        "match_radius_px", "centroid_mean_px", "centroid_p95_px",
        "missed_fraction", "extra_per_event", "count_mismatch_fraction",
    ):
        assert getattr(board, field) >= getattr(host, field), field


def test_the_committed_report_is_the_one_the_generator_writes():
    """The report is a BUILD PRODUCT, and nothing checked that it was rebuilt.

    D8.0a rewrote the generator to describe the amendment and the committed
    `D8_EDGE_REPORT.md` went on asserting the superseded constant — the code
    said one thing, the shipped deliverable said the opposite, and the suite
    stayed green. That is D8-F6 again, one level up: the same class of
    divergence, this time between a generator and its artifact rather than
    between two implementations of a value.

    Deterministic to assert, because `generate` promises byte-identity for
    identical inputs and reads only in-repo files — no clock, no environment,
    no toolchain. Both inputs (the committed fixtures and the evidence file)
    are in the repo, so this either passes everywhere or fails everywhere.

    It does NOT check that the EVIDENCE is fresh — only `measure` can do that,
    and it needs a built daemon. What it guarantees is that the document in
    the repo is the document this code produces from the evidence in the repo.
    """
    import json
    from pathlib import Path

    from skyweave2.edge import report as report_module

    docs = Path(__file__).resolve().parents[2] / "docs"
    committed = docs / "D8_EDGE_REPORT.md"
    if not committed.exists():
        pytest.skip("D8_EDGE_REPORT.md has not been written yet")
    evidence_path = docs / "d8_evidence.json"
    evidence = (
        json.loads(evidence_path.read_text(encoding="utf-8"))
        if evidence_path.exists()
        else None
    )
    assert committed.read_text(encoding="utf-8") == report_module.generate(evidence), (
        "docs/D8_EDGE_REPORT.md is not what the generator writes. Re-run:\n"
        "  uv run python -m skyweave2.edge.report measure --out docs/d8_evidence.json\n"
        "  uv run python -m skyweave2.edge.report generate "
        "--evidence docs/d8_evidence.json --out docs/D8_EDGE_REPORT.md\n"
        "(measure first, and with a freshly built daemon: it is what refreshes "
        "the suite counts and the binary's SHA-256.)"
    )


def test_the_declared_tolerances_are_the_ones_the_report_quotes():
    """The report is where the bounds are DECLARED; this module is where they
    are enforced. If the two ever disagree, the declaration is fiction."""
    from pathlib import Path

    report = Path(__file__).resolve().parents[2] / "docs" / "D8_EDGE_REPORT.md"
    if not report.exists():
        pytest.skip("D8_EDGE_REPORT.md has not been written yet")
    text = report.read_text(encoding="utf-8")

    # Parsed out of the two tolerance TABLES, not searched for anywhere in
    # the document. A bare substring test passes on any number that happens
    # to appear somewhere in 400 lines of prose — "0.5" occurs in half a
    # dozen unrelated sentences — so it would have accepted a report that
    # declared bounds nobody enforces. Section 6's tables are the
    # declaration; this reads them.
    sections = {
        "soft": _table_rows(text, "### 6.1"),
        "ive": _table_rows(text, "### 6.2"),
    }
    labels = {
        "match_radius_px": "match radius (full-res px)",
        "centroid_mean_px": "centroid mean (full-res px)",
        "centroid_p95_px": "centroid p95 (full-res px)",
        "missed_fraction": "missed fraction",
        "extra_per_event": "extra per capture event",
        "count_mismatch_fraction": "count-mismatch fraction",
    }
    for key, declared in (
        ("soft", tolerance.HOST_SOFT_TOLERANCE),
        ("ive", tolerance.BOARD_IVE_TOLERANCE),
    ):
        rows = sections[key]
        assert rows, f"the report has no section 6 tolerance table for {key}"
        for field, label in labels.items():
            assert label in rows, f"{key}: the report's table omits {label}"
            assert rows[label] == f"{getattr(declared, field):g}", (
                f"{key} {field}: the report declares {rows[label]}, the code "
                f"enforces {getattr(declared, field):g}. A declaration that "
                "disagrees with the gate is not a declaration."
            )


def _table_rows(text: str, heading: str) -> dict:
    """{axis label: declared bound} from the markdown table under `heading`."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    except StopIteration:
        return {}
    rows = {}
    for line in lines[start:]:
        if line.startswith("###") and not line.startswith(heading):
            break
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] not in ("Axis", "---"):
            rows[cells[0]] = cells[1]
    return rows
