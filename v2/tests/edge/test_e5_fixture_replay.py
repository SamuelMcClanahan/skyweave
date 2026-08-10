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
