"""E2: nanopb byte-identity against the host codec, both directions.

The absolute half of the D8 fixture policy: for IDENTICAL observation inputs
the daemon's nanopb encoding must be byte-identical to the host codec's.
There is no tolerance here and there cannot be — a wire the two sides
disagree about is not a wire.

"Identical inputs" is doing real work. Both encoders are handed the same
``observations.swob`` bytes: the host through
``skyweave2.edge.obsfixture.decode_fixture``, the C daemon through
``sw_obsfixture.c``. A JSON fixture would have let a reader's rounding
masquerade as an encoder's disagreement.

The adversarial fixture (``wire_case_events``) is what makes this a gate
rather than a formality: an all-zero bbox, absent-vs-zero optionals, a
negative capture timestamp, saturated strings and a full event at the
declared bound are each a distinct way for two plausible encoders to differ
while a clip-shaped fixture agrees on everything.
"""

from __future__ import annotations

import io
import subprocess

import pytest

from skyweave2.edge import daemon, fixtures
from skyweave2.edge.obsfixture import (
    decode_fixture,
    encode_fixture,
    group_by_capture_event,
)
from skyweave2.transport import codec
from skyweave2.transport.wire import ProtocolViolation, unframe
from tests.edge.conftest import COMMITTED_FIXTURES, load_events, load_observations

# ---------------------------------------------------------------------------
# The fixture format itself: shared input, not a Python-shaped one
# ---------------------------------------------------------------------------


def test_the_binary_fixture_round_trips_through_the_wire_shape():
    """SWOB stores the D0-declared `float` fields as binary32, so a round
    trip equals `wire_normalize`, not the input. That is D7-F2 reproduced
    deliberately: a fixture carrying doubles would hand the two encoders
    values the wire cannot represent."""
    events = fixtures.wire_case_events()
    decoded = decode_fixture(io.BytesIO(encode_fixture(events)))
    expected = [
        (codec.wire_normalize(observations[0]).envelope,
         [codec.wire_normalize(o) for o in observations])
        for _, observations in events
    ]
    assert decoded == expected


def test_the_jsonl_twin_describes_the_same_observations():
    """The binary is what the encoders eat; the JSONL is what a reviewer
    reads. If they ever disagreed, the reviewable artifact would be fiction."""
    import json

    for name in COMMITTED_FIXTURES:
        binary = load_observations(name)
        lines = (
            fixtures.FIXTURE_ROOT / name / "observations.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        assert len(binary) == len(lines), name
        for observation, line in zip(binary, lines, strict=True):
            recorded = json.loads(line)
            # The JSONL is written from the pre-wire model, so the declared
            # float fields are compared through the same narrowing.
            assert observation == codec.wire_normalize(
                type(observation).model_validate(recorded)
            ), f"{name} obs {recorded['obs_id']}"


def test_the_committed_stats_have_the_schema_write_fixture_actually_writes(tmp_path):
    """`stats.json` is the one committed artifact nothing else derives.

    It is provenance, and `report.py` prints four of its numbers straight into
    the report's fixture table — so a stats file that has drifted from its
    writer publishes fiction with a measured-looking table around it. The
    D8.0a regeneration is what surfaced this: the committed files predated the
    `--seed` work, so regenerating them ADDED a key, which means the repo had
    been carrying stats files their own `write_fixture` could not produce and
    no test said so.

    Keys only, never values: `environment` pins library versions and
    `session_uuid` is per fixture, so comparing values would fail on a machine
    the fixtures are fine on. The writer is driven for real (through the
    adversarial wire-cases build, which needs no clip and no detector) rather
    than transcribed here, so the expected schema cannot drift from the code.
    """
    import json

    written = json.loads(
        (
            fixtures.write_fixture(fixtures.build_wire_cases(), tmp_path / "probe")
            / "stats.json"
        ).read_text(encoding="utf-8")
    )
    for name in COMMITTED_FIXTURES:
        committed = json.loads(
            (fixtures.FIXTURE_ROOT / name / "stats.json").read_text(encoding="utf-8")
        )
        assert sorted(committed) == sorted(written), f"{name}: stats.json schema"
        assert sorted(committed["detector"]) == sorted(written["detector"]), name
        assert sorted(committed["environment"]) == sorted(written["environment"]), name


def test_the_committed_stats_counters_agree_with_the_byte_gated_artifacts():
    """Make the report's fixture table derived rather than trusted.

    Every number here is recomputable from `observations.swob` and
    `packets.hex`, which ARE the absolute byte gate, so this costs nothing and
    turns four published figures into checked ones. Without it a wholly
    falsified `stats.json` passes the entire suite — and `gate/stats.json`, in
    particular, is read by no other test at all.
    """
    from tests.edge.conftest import load_packets, load_stats

    for name in COMMITTED_FIXTURES:
        events = load_events(name)
        observations = load_observations(name)
        packets = load_packets(name)
        stats = load_stats(name)
        assert stats["capture_events"] == len(events), name
        assert stats["observations"] == len(observations), name
        assert stats["max_observations_per_event"] == max(
            len(event) for _, event in events
        ), name
        assert stats["packet_bytes_max"] == max(len(p) for p in packets), name
        assert stats["detector"]["components_emitted"] == len(observations), name


def test_committed_packets_are_what_the_host_codec_produces():
    """The Python half of the byte gate, which runs without a C toolchain."""
    from tests.edge.conftest import load_packets

    for name in COMMITTED_FIXTURES:
        produced = [
            codec.encode_observation_packet(envelope, observations)
            for envelope, observations in load_events(name)
        ]
        assert produced == load_packets(name), name


# ---------------------------------------------------------------------------
# Encode: C nanopb vs the host codec
# ---------------------------------------------------------------------------


def _c_encode(build_dir, fixture_path) -> list[bytes]:
    completed = subprocess.run(
        [str(daemon.fixture_tool_path(build_dir)), "encode", str(fixture_path)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return [bytes.fromhex(line) for line in completed.stdout.split() if line]


def _c_decode(build_dir, packets_path) -> list[str]:
    completed = subprocess.run(
        [str(daemon.fixture_tool_path(build_dir)), "decode", str(packets_path)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.splitlines()


@pytest.mark.parametrize("name", COMMITTED_FIXTURES)
def test_nanopb_encoding_is_byte_identical_to_the_host_codec(edge_build_dir, name):
    from tests.edge.conftest import load_packets

    produced = _c_encode(edge_build_dir, fixtures.FIXTURE_ROOT / name / "observations.swob")
    expected = load_packets(name)
    assert len(produced) == len(expected), f"{name}: datagram count"
    for index, (got, want) in enumerate(zip(produced, expected, strict=True)):
        assert got == want, (
            f"{name} datagram {index}: nanopb and the host codec disagree.\n"
            f"  nanopb: {got.hex()}\n  host:   {want.hex()}"
        )


def test_nanopb_encoding_holds_on_the_adversarial_cases(edge_build_dir, tmp_path):
    """The cases no clip produces: empty-but-present bbox, absent vs zero,
    negative timestamps and bbox origins, saturated strings, a full event."""
    events = fixtures.wire_case_events()
    path = tmp_path / "wirecases.swob"
    path.write_bytes(encode_fixture(events))
    expected = [
        codec.encode_observation_packet(envelope, observations)
        for envelope, observations in events
    ]
    produced = _c_encode(edge_build_dir, path)
    assert len(produced) == len(expected)
    for index, (got, want) in enumerate(zip(produced, expected, strict=True)):
        assert got == want, (
            f"wire case {index} diverged.\n  nanopb: {got.hex()}\n  host:   {want.hex()}"
        )


def test_the_adversarial_fixture_really_exercises_the_hard_cases():
    """Can-fail guard: if these cases quietly stopped being present, the test
    above would keep passing while testing nothing interesting."""
    events = fixtures.wire_case_events()
    flat = [observation for _, observations in events for observation in observations]
    assert any(o.bbox_x == 0 and o.bbox_y == 0 for o in flat), "no zero-origin bbox"
    assert any(o.local_blob_id is None for o in flat), "no absent local_blob_id"
    assert any(o.local_blob_id == 0 for o in flat), "no zero local_blob_id"
    assert any(o.evidence_ref is None for o in flat), "no absent evidence_ref"
    assert any(o.evidence_ref == "" for o in flat), "no empty evidence_ref"
    assert any(o.bbox_x < 0 for o in flat), "no negative bbox origin"
    assert any(o.envelope.capture_ts_ns < 0 for _, obs in events for o in obs), (
        "no negative capture timestamp"
    )
    assert any(len(observations) == 7 for _, observations in events), "no full event"
    assert any(len(o.envelope.session_uuid) == 36 for o in flat), "no saturated uuid"


# ---------------------------------------------------------------------------
# Decode: the other direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMMITTED_FIXTURES)
def test_nanopb_decodes_the_committed_packets_to_the_same_values(edge_build_dir, name):
    """Both directions, per the brief. Encoding agreement alone would let a
    matched pair of decode bugs survive."""
    lines = _c_decode(edge_build_dir, fixtures.FIXTURE_ROOT / name / "packets.hex")
    expected = load_observations(name)
    assert len(lines) == len(expected), f"{name}: observation count"
    for line, observation in zip(lines, expected, strict=True):
        fields = dict(item.split("=", 1) for item in line.split(" ") if "=" in item)
        assert int(fields["obs"]) == observation.obs_id
        assert float(fields["u"]) == observation.u
        assert float(fields["v"]) == observation.v
        assert float(fields["cuu"]) == observation.cov_uu
        assert float(fields["cuv"]) == observation.cov_uv
        assert float(fields["cvv"]) == observation.cov_vv
        assert int(fields["seq"]) == observation.envelope.frame_seq
        assert int(fields["ts"]) == observation.envelope.capture_ts_ns
        assert fields["uuid"] == observation.envelope.session_uuid
        assert int(fields["area"]) == observation.area_px
        assert int(fields["persist"]) == observation.persistence_count
        expected_blob = (
            "absent:0" if observation.local_blob_id is None
            else str(observation.local_blob_id)
        )
        assert fields["blob"] == expected_blob


def test_nanopb_decode_preserves_absent_versus_zero(edge_build_dir, tmp_path):
    """T7/W3's rule at the C boundary: dropping evidence is representable as
    ABSENCE, never as an empty string, and blob 0 is not "no blob"."""
    events = fixtures.wire_case_events()
    path = tmp_path / "cases.hex"
    path.write_text(
        "\n".join(
            codec.encode_observation_packet(envelope, observations).hex()
            for envelope, observations in events
        )
        + "\n",
        encoding="utf-8",
    )
    lines = _c_decode(edge_build_dir, path)
    flat = [observation for _, observations in events for observation in observations]
    assert len(lines) == len(flat)
    for line, observation in zip(lines, flat, strict=True):
        if observation.evidence_ref is None:
            assert "evidence=absent:" in line
        else:
            assert f"evidence={observation.evidence_ref}" in line
        if observation.local_blob_id is None:
            assert "blob=absent:0" in line
        else:
            assert f"blob={observation.local_blob_id}" in line


# ---------------------------------------------------------------------------
# Unknown-field tolerance (D0 T5), at the C boundary
# ---------------------------------------------------------------------------


def test_nanopb_tolerates_a_newer_peers_unknown_fields(edge_build_dir, tmp_path):
    """Forward compatibility. nanopb SKIPS unknown fields by default; this
    proves it rather than trusting the documentation, because a decoder that
    rejected them would make every future schema addition a fleet outage."""
    from skyweave2.transport.wire import PayloadType, frame
    from tests.transport.test_w1_schema import _unknown_field_bytes

    envelope, observations = load_events("sparse")[0]
    packet = codec.build_observation_packet(envelope, observations)
    packet.observations[0].MergeFromString(_unknown_field_bytes(900, 42))
    packet.envelope.MergeFromString(_unknown_field_bytes(901, 7))
    body = packet.SerializeToString(deterministic=True)
    clean = codec.build_observation_packet(envelope, observations).SerializeToString(
        deterministic=True
    )
    assert len(body) > len(clean), "the unknown fields were not actually in the bytes"

    path = tmp_path / "unknown.hex"
    path.write_text(frame(PayloadType.OBSERVATION, body).hex() + "\n", encoding="utf-8")
    lines = _c_decode(edge_build_dir, path)
    assert len(lines) == len(observations)
    # Every KNOWN field still arrives with its value: "it did not crash" is not
    # tolerance, it is survival. A decoder that skipped the unknown field by
    # losing its place would still produce a line, and the numbers would move.
    for line, observation in zip(lines, observations, strict=True):
        fields = dict(item.split("=", 1) for item in line.split(" ") if "=" in item)
        assert float(fields["u"]) == observation.u
        assert float(fields["v"]) == observation.v
        assert int(fields["obs"]) == observation.obs_id
        assert int(fields["seq"]) == observation.envelope.frame_seq
        assert int(fields["area"]) == observation.area_px


def test_nanopb_rejects_a_per_observation_envelope(edge_build_dir, tmp_path):
    """The envelope-once rule, enforced on the C side too. A daemon that
    accepted it would let a peer rewrite capture time (D0 section 3)."""
    from skyweave2.transport.wire import PayloadType, frame

    envelope, observations = load_events("sparse")[0]
    packet = codec.build_observation_packet(envelope, observations)
    packet.observations[0].envelope.camera_id = 9
    path = tmp_path / "bad.hex"
    path.write_text(
        frame(PayloadType.OBSERVATION,
              packet.SerializeToString(deterministic=True)).hex() + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(daemon.fixture_tool_path(edge_build_dir)), "decode", str(path)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode != 0
    assert "own envelope" in completed.stderr
    # ...and the host rejects the identical bytes for the identical reason.
    with pytest.raises(ProtocolViolation, match="own envelope"):
        codec.decode_observation_packet(
            unframe(bytes.fromhex(path.read_text().strip()))[1]
        )


def test_grouping_into_capture_events_is_stable():
    """A regrouped stream must reproduce the fixture's events exactly; a
    grouping that merged non-adjacent runs would pack packets the live path
    never builds."""
    for name in COMMITTED_FIXTURES:
        events = load_events(name)
        flat = [observation for _, observations in events for observation in observations]
        assert group_by_capture_event(flat) == events, name
