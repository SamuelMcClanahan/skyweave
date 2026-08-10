"""E1: the D8 capacity decision, end to end.

Four things the D0 "D8 opening" entry promised, each tested where it can
actually fail:

- the ceiling, the wire ``max_count`` and the detector cap AGREE, and the
  count is DERIVED from the ceiling rather than chosen;
- the invariant ``wire max_count >= detector cap`` holds, in Python and in
  the daemon's own startup check;
- a cluttered frame emits the top ``cap`` components and COUNTS the drops;
- an event at the cap is always encodable, at the worst case the schema
  permits and not merely on a clean clip.

Plus the thing the brief is most explicit about: the existing golden byte
fixtures must NOT change. ``max_count`` is an allocation bound, not a wire
value, so raising it must move no byte anywhere.
"""

from __future__ import annotations

import subprocess

import pytest

from skyweave2.detector.cap import (
    CONFIDENCE_SATURATION_AREA_PX,
    DetectorStats,
    apply_component_cap,
    component_confidence,
    rank_key,
)
from skyweave2.detector.components import MaskComponent
from skyweave2.detector.config import DetectorConfig
from skyweave2.detector.runner import detect_clip
from skyweave2.edge import daemon
from skyweave2.transport import codec, sizing
from skyweave2.transport.wire import (
    DATAGRAM_CEILING_BYTES,
    WIRE_LIMITS,
    TooManyObservations,
    declared_limits_from_options,
)
from tests.edge.conftest import (
    COMMITTED_FIXTURES,
    load_config,
    load_events,
    load_observations,
    load_stats,
)

# ---------------------------------------------------------------------------
# The three numbers agree, and the count is derived
# ---------------------------------------------------------------------------


def test_the_ceiling_is_the_untagged_ethernet_mtu_payload():
    """1500 - 20 (IPv4) - 8 (UDP). Written as the arithmetic, not as 1472,
    so the reason survives the number."""
    assert DATAGRAM_CEILING_BYTES == 1500 - 20 - 8


def test_wire_bound_is_derived_from_the_ceiling_not_chosen():
    derived = sizing.max_observations_under_ceiling()
    assert WIRE_LIMITS.observations_max_count == derived == 7
    assert sizing.worst_case_datagram_size(derived) <= DATAGRAM_CEILING_BYTES
    assert sizing.worst_case_datagram_size(derived + 1) > DATAGRAM_CEILING_BYTES


def test_options_file_and_python_limits_still_agree():
    """The C daemon allocates from the .options file and the host from
    WireLimits; they are one declaration or they are a trap."""
    assert declared_limits_from_options() == WIRE_LIMITS


def test_the_invariant_wire_bound_is_at_least_the_detector_cap():
    """THE invariant of the capacity decision.

    The wire bound is what the peer's nanopb ALLOCATES from. A detector cap
    above it would build capture events this host is happy to encode and the
    board cannot decode — a divergence that only appears on a cluttered
    frame, in the field.
    """
    assert WIRE_LIMITS.observations_max_count >= DetectorConfig().max_components_per_frame


def test_a_cap_above_the_wire_bound_would_actually_break_encoding():
    """Can-fail guard: prove the invariant is load-bearing, not decorative."""
    envelope_events = load_events("clutter")
    envelope, observations = envelope_events[0]
    over = observations + [
        observations[0].model_copy(update={"obs_id": 900 + index})
        for index in range(WIRE_LIMITS.observations_max_count)
    ]
    assert len(over) > WIRE_LIMITS.observations_max_count
    with pytest.raises(TooManyObservations):
        codec.encode_observation_packet(envelope, over)


# ---------------------------------------------------------------------------
# The cap keeps the top components and counts the rest
# ---------------------------------------------------------------------------


def _component(area: int, u: float, v: float) -> MaskComponent:
    return MaskComponent(
        centroid_u=u, centroid_v=v, area_px=area,
        bbox_x=int(u), bbox_y=int(v), bbox_w=2, bbox_h=2,
    )


def test_cap_keeps_the_top_components_and_counts_the_drops():
    emitted = [(_component(area, float(area), 0.0), 2, area) for area in range(1, 11)]
    result = apply_component_cap(emitted, 7)
    assert len(result.kept) == 7
    assert result.dropped == 3
    assert result.offered == 10
    assert result.at_cap
    # Largest seven areas survive: 4..10.
    assert sorted(c.area_px for c, _, _ in result.kept) == [4, 5, 6, 7, 8, 9, 10]


def test_cap_survivors_keep_their_offered_order():
    """obs_id is assigned by position; rank order must not renumber a frame.

    The areas are arranged so that OFFERED order and RANK order are
    genuinely different sequences: the survivors are the four largest, whose
    offered positions are 0, 3, 5, 7, while rank order would put them at
    3, 0, 7, 5. Asserting "the kept list is sorted by position" against a
    fixture whose top-N happen to be ascending in both orders proves
    nothing — the first version of this test did exactly that.
    """
    areas = [9, 1, 2, 12, 3, 7, 4, 8]
    emitted = [(_component(area, float(index), 0.0), 2, index)
               for index, area in enumerate(areas)]
    result = apply_component_cap(emitted, 4)
    kept_positions = [c.centroid_u for c, _, _ in result.kept]
    assert kept_positions == [0.0, 3.0, 5.0, 7.0], "survivors were re-ordered by rank"
    rank_order = [
        component.centroid_u
        for component, _, _ in sorted(result.kept, key=rank_key)
    ]
    assert rank_order != kept_positions, (
        "this fixture no longer distinguishes offered order from rank order, so "
        "the assertion above has stopped testing anything"
    )


def test_cap_is_a_no_op_below_the_bound():
    emitted = [(_component(5, 1.0, 1.0), 2, 0)]
    result = apply_component_cap(emitted, 7)
    assert result.kept == emitted
    assert result.dropped == 0
    assert not result.at_cap


def test_the_ranking_is_total_so_the_choice_is_never_positional():
    """Equal confidence AND equal area must still order deterministically.

    Without the raster tie-break the survivors would be decided by list
    position, which is stable in CPython and meaningless as a rule — and the
    C daemon would have to reproduce an accident of Python's sort.
    """
    a = (_component(5, 1.0, 2.0), 2, 0)
    b = (_component(5, 3.0, 2.0), 2, 1)
    c = (_component(5, 0.0, 1.0), 2, 2)
    assert rank_key(c) < rank_key(a) < rank_key(b)
    result = apply_component_cap([a, b, c], 2)
    assert [comp.centroid_u for comp, _, _ in result.kept] == [1.0, 0.0]


def test_confidence_is_the_declared_area_formula():
    """The D0 "D8.0 amendment" entry, transcribed: `min(1.0, area_px / 50.0)`.

    Written as the arithmetic and checked at the saturation boundary, not as
    a table of blessed outputs, so a re-interpretation of the formula (a
    threshold instead of a ramp, a different divisor, a persistence term)
    fails here rather than in a fixture diff nobody reads.
    """
    assert CONFIDENCE_SATURATION_AREA_PX == 50.0
    for area in (1, 2, 12, 25, 49, 50, 51, 100, 10_000):
        expected = min(1.0, area / 50.0)
        assert component_confidence(_component(area, 0.0, 0.0), 2) == expected, area
    # The boundary, spelled out: it saturates AT 50, not after it.
    assert component_confidence(_component(49, 0.0, 0.0), 2) == 0.98
    assert component_confidence(_component(50, 0.0, 0.0), 2) == 1.0
    assert component_confidence(_component(51, 0.0, 0.0), 2) == 1.0


def test_confidence_never_saturates_past_one_and_never_reaches_zero():
    """Two bounds with teeth.

    Above 1.0 would be a value the D0 contract's 0..1 field does not admit.
    Exactly 0.0 is worse and less obvious: `confidence` is a proto3 scalar
    with implicit presence, so a zero would be OMITTED from the encoding
    entirely — an observation that silently loses a field rather than one
    that carries a small number. `min_area_px >= 1` is what makes that
    unreachable, so the smallest legal component is the case tested.
    """
    assert component_confidence(_component(10_000_000, 0.0, 0.0), 2) == 1.0
    smallest = DetectorConfig().min_area_px
    assert smallest >= 1
    assert component_confidence(_component(smallest, 0.0, 0.0), 2) > 0.0


def test_persistence_does_not_enter_the_confidence():
    """It is already its own wire field; folding it in would double-count it
    in a value the fusion side may later weight by."""
    component = _component(12, 0.0, 0.0)
    assert component_confidence(component, 2) == component_confidence(component, 900)


def test_confidence_is_the_first_rank_level_and_never_contradicts_area():
    """D8-F1, made checkable.

    Confidence is the first level of `rank_key` and is now an actual
    quantity. It is also a monotone non-decreasing function of area, which is
    the second level — so the two can tie but can never DISAGREE, and the
    cap's selection is the one area alone would make. That is the claim the
    report makes and the claim the C daemon's component-shedding order
    (`sw_detect_soft.c::rank_worse_than`, which sorts by area alone) depends
    on being true.
    """
    small = (_component(10, 0.0, 0.0), 2, 0)
    big = (_component(100, 1.0, 0.0), 2, 1)
    assert rank_key(small)[0] == -component_confidence(*small[:2])
    assert rank_key(big)[0] == -component_confidence(*big[:2])
    assert rank_key(big) < rank_key(small)

    # Monotone, and the tie above saturation is real: 60 and 100 px are both
    # confidence 1.0 and are separated by area, not by confidence.
    areas = [1, 2, 12, 25, 49, 50, 51, 60, 100, 1000]
    confidences = [component_confidence(_component(a, 0.0, 0.0), 2) for a in areas]
    assert confidences == sorted(confidences)
    assert component_confidence(_component(60, 0.0, 0.0), 2) == component_confidence(
        _component(100, 0.0, 0.0), 2
    )
    saturated = [(_component(100, 0.0, 0.0), 2, 0), (_component(60, 1.0, 0.0), 2, 1)]
    assert rank_key(saturated[0]) < rank_key(saturated[1]), "area must break the tie"


def test_the_amendment_did_not_change_which_components_the_cap_keeps():
    """The regeneration moved a wire VALUE, not a selection.

    This is the property that makes the D8.0a fixture regeneration a
    confidence-field change and nothing else, and it is worth pinning: if a
    future confidence model stops being a function of area, this test fails
    and the person changing it has to state that the cap now selects
    differently — which is a fusion-visible change, not a field edit.
    """
    areas = [3, 61, 12, 61, 7, 200, 12, 49, 50, 51, 4, 88]
    emitted = [
        (_component(area, float(index % 5), float(index)), 2, index)
        for index, area in enumerate(areas)
    ]
    # Areas above and below the saturation point, and duplicates, so both the
    # "confidence decides" and the "confidence ties, area decides" halves of
    # the ranking are exercised at once.
    assert any(area >= 50 for area in areas) and any(area < 50 for area in areas)
    by_area = sorted(
        emitted,
        key=lambda item: (-item[0].area_px, item[0].centroid_v, item[0].centroid_u),
    )[:7]
    kept = apply_component_cap(emitted, 7).kept
    assert {blob for _, _, blob in kept} == {blob for _, _, blob in by_area}


def test_cap_of_zero_is_refused_not_silently_treated_as_unlimited():
    with pytest.raises(ValueError, match="at least 1"):
        apply_component_cap([(_component(1, 0.0, 0.0), 2, 0)], 0)


def test_the_confidence_on_the_wire_is_the_one_the_cap_ranks_by(scene_clips):
    """The ranked quantity and the REPORTED quantity must be one quantity.

    The cap's whole justification is "keep the top components by descending
    confidence". If the runner put a different number on the wire than
    `cap.py` ranked by, the cap would be selecting on one thing while the
    Jetson read another, and both would look reasonable in isolation.

    It is also a HOST/EDGE gate. The C daemon's `sw_pipeline.c` calls its own
    `component_confidence()` for both purposes, so the two sides agree only
    while this side does too — and a divergence here would not show up in
    E2 or E5, which compare against COMMITTED fixture bytes rather than a
    fresh oracle run. This test is the one that would notice.
    """
    config = load_config("sparse")
    observations, frames = detect_clip(scene_clips["sparse"], config)
    assert observations, "no observations to check"
    emitted = [item for frame in frames for item in frame.emitted]
    assert len(emitted) == len(observations)
    for observation, (component, count, _blob) in zip(
        observations, emitted, strict=True
    ):
        assert observation.confidence == component_confidence(component, count), (
            "the confidence the detector REPORTS is not the confidence the cap "
            "RANKS BY; the cap would be selecting on a different quantity than "
            "the one the Jetson receives"
        )


def test_the_committed_confidence_is_the_declared_function_of_the_committed_area():
    """The two hand-back tests above compare the detector against ITSELF.

    Both would still pass if `component_confidence` regressed to a constant
    and the fixtures were regenerated to match: reported would equal ranked,
    and a fresh oracle would reproduce the (new) committed bytes. This is the
    check that reads the committed artifact on its own terms — every
    observation's `confidence` against its own `area_px`, through the
    formula the D0 amendment names — and it needs no toolchain and no clip.

    The non-vacuity assertion is the other half: the fixtures must carry more
    than one distinct confidence, or a constant would satisfy the formula on
    a fixture where every component happened to be the same size.
    """
    distinct = set()
    for name in COMMITTED_FIXTURES:
        observations = load_observations(name)
        assert observations, name
        for observation in observations:
            expected = codec.to_float32(
                min(1.0, observation.area_px / CONFIDENCE_SATURATION_AREA_PX)
            )
            assert observation.confidence == expected, (
                f"{name} obs {observation.obs_id} @ frame "
                f"{observation.envelope.frame_seq}: confidence "
                f"{observation.confidence} is not min(1, {observation.area_px}/50)"
            )
            distinct.add(observation.confidence)
    assert len(distinct) > 1, (
        "every committed observation carries the same confidence, so the "
        "formula check above would also pass for a constant"
    )


def test_the_committed_fixtures_still_match_a_fresh_oracle_run(scene_clips):
    """The committed fixtures ARE the host oracle's output, or they are stale.

    E2 and E5 both feed on the committed bytes, so a change to the detector
    that nobody regenerated for would leave every byte gate passing against
    a fixture that no longer describes this detector. This is the test that
    connects the two: run the oracle now, and require the bytes it produces
    to be the bytes in the repo.

    If this fails, either the detector changed (regenerate with a recorded
    reason, per the golden policy) or something changed the detector by
    accident. Both are worth stopping for.
    """
    for name in ("sparse", "clutter"):
        from skyweave2.edge import fixtures as edge_fixtures

        build = edge_fixtures.build_fixture(
            name, scene_clips[name], load_config(name)
        )
        committed = [
            bytes.fromhex(line)
            for line in (
                edge_fixtures.FIXTURE_ROOT / name / "packets.hex"
            ).read_text(encoding="utf-8").split()
            if line
        ]
        assert build.packets == committed, (
            f"{name}: a fresh oracle run no longer reproduces the committed "
            "packet bytes. The fixtures are stale, or the detector moved."
        )


# ---------------------------------------------------------------------------
# The cluttered fixture: the cap bites, and says so
# ---------------------------------------------------------------------------


def test_the_clutter_fixture_actually_exceeds_the_cap():
    """A cap fixture that stopped exceeding the cap would test nothing."""
    stats = load_stats("clutter")["detector"]
    cap = load_config("clutter").max_components_per_frame
    assert stats["max_components_offered"] > cap
    assert stats["components_dropped_over_cap"] > 0
    assert stats["frames_at_cap"] > 0


def test_every_committed_event_is_within_the_cap_and_the_wire_bound():
    for name in ("sparse", "clutter", "gate"):
        cap = load_config(name).max_components_per_frame
        for envelope, observations in load_events(name):
            assert len(observations) <= cap, f"{name} {envelope.frame_seq}"
            assert len(observations) <= WIRE_LIMITS.observations_max_count


def test_drops_are_counted_not_inferred_from_the_emitted_count(scene_clips):
    """The counters and the emitted stream must agree, and the count must
    come from the cap rather than from a subtraction that would also absorb
    a persistence-filter drop."""
    stats = DetectorStats()
    config = load_config("clutter")
    observations, frames = detect_clip(scene_clips["clutter"], config, stats=stats)
    assert stats.components_emitted == len(observations)
    assert stats.components_offered == (
        stats.components_emitted + stats.components_dropped_over_cap
    )
    per_frame_drops = sum(frame.dropped_over_cap for frame in frames)
    assert per_frame_drops == stats.components_dropped_over_cap > 0
    for frame in frames:
        assert frame.offered == len(frame.emitted) + frame.dropped_over_cap


def test_an_event_at_the_cap_is_always_encodable_at_the_schema_worst_case():
    """Not "on this clip": at every declared bound saturated at once."""
    budget = sizing.worst_case_budget(DetectorConfig().max_components_per_frame)
    assert budget.fits, (
        f"a full capture event is {budget.total_bytes} B against a "
        f"{budget.ceiling_bytes} B ceiling"
    )
    envelope = sizing.worst_case_envelope()
    observations = [
        sizing.worst_case_observation(envelope, obs_id)
        for obs_id in range(DetectorConfig().max_components_per_frame)
    ]
    datagram = codec.encode_observation_packet(envelope, observations)
    assert len(datagram) <= DATAGRAM_CEILING_BYTES


# ---------------------------------------------------------------------------
# The daemon enforces the same invariant, at startup
# ---------------------------------------------------------------------------


def test_the_daemon_refuses_a_cap_above_the_wire_bound(edge_build_dir, tmp_path):
    """The C side must refuse at STARTUP, not discover it on a busy frame."""
    over = WIRE_LIMITS.observations_max_count + 1
    completed = subprocess.run(
        [str(daemon.daemon_path(edge_build_dir)), "--inject-file",
         str(tmp_path / "absent.swij"), "--cap", str(over)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode != 0
    assert "exceeds the declared wire bound" in completed.stderr


def test_the_daemon_reports_the_same_bound_this_host_enforces(edge_build_dir, tmp_path):
    """The daemon's own view of the wire bound comes from the generated
    nanopb header, i.e. from proto/skyweave.options. If it ever disagreed
    with WireLimits, the two sides would be sized against different numbers
    and nothing else in the suite would notice."""
    stream = tmp_path / "empty.swij"
    stream.write_bytes(b"")
    completed = subprocess.run(
        [str(daemon.daemon_path(edge_build_dir)), "--inject-file", str(stream),
         "--cap", str(WIRE_LIMITS.observations_max_count)],
        capture_output=True, text=True, check=False,
    )
    # The stream is empty so the run fails; the point is that it got PAST
    # validation with a cap equal to the bound.
    assert "exceeds the declared wire bound" not in completed.stderr


def test_the_daemon_agrees_with_the_host_about_the_bound(edge_build_dir, tmp_path,
                                                         scene_clips):
    from skyweave2.edge.injection import build_injection_stream

    config = load_config("clutter")
    stream = tmp_path / "clutter.swij"
    stream.write_bytes(
        build_injection_stream(scene_clips["clutter"], config, "e1-bound-check")
    )
    run = daemon.run_daemon_on_stream(stream, config, tmp_path / "run",
                                      build_dir=edge_build_dir)
    assert run.returncode == 0, run.stderr
    assert run.stats["wire_observations_max_count"] == WIRE_LIMITS.observations_max_count
    assert run.stats["max_components_per_frame"] == config.max_components_per_frame
    assert run.stats["components_dropped_over_cap"] > 0
    assert run.stats["events_unencodable"] == 0
    # The health path carries the drops, per the brief. Never silent.
    assert run.stats["health_drops_total"] >= run.stats["components_dropped_over_cap"]


# ---------------------------------------------------------------------------
# The golden byte fixtures did not move
# ---------------------------------------------------------------------------


def test_raising_max_count_moved_no_wire_byte():
    """max_count is an ALLOCATION bound, not a wire value.

    The D7 golden datagrams are re-encoded here from their own fixtures and
    compared against the committed hex. Those files are also gated by W1;
    this test states the D8-specific claim — that the capacity change was
    free on the wire — where a D8 reader will look for it.
    """
    from tests.transport.test_w1_schema import (
        GOLDEN_DIR,
        canonical_envelope,
        canonical_observation,
    )

    envelope = canonical_envelope()
    produced = codec.encode_observation_packet(
        envelope, [canonical_observation(envelope)]
    ).hex()
    stored = (GOLDEN_DIR / "observation_single.hex").read_text(encoding="utf-8").strip()
    assert produced == stored
