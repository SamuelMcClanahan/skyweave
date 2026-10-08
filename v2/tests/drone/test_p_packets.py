"""P series: JSON wire v1 (DRONE_CONTRACTS_D0.md §1-§2, FROZEN).

The load-bearing assertions are the golden BYTES, not the round-trip:
encoding and decoding with the same module proves little on its own. Each
golden file in ``golden/`` pins the exact canonical datagram ([C5]) for one
packet kind, so any change to field names, key order, number formatting, or
enum spelling fails here even when a round-trip would still pass.

Regeneration is env-gated, mirroring the repo's golden policy: pinned bytes
change only with a recorded reason (a decision appended to contract §11).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from skyweave2.drone import packets as P

GOLDEN = Path(__file__).resolve().parent / "golden"
REGENERATE = os.environ.get("SKYWEAVE_REGENERATE_DRONE_GOLDEN") == "1"

TRIAL = P.TrialEcho(
    trial_type=P.TrialType.TOUCH,
    v_max=2.5,
    alpha=0.4,
    beta=0.1,
    k=5,
    pass_budget=1,
    search_alt=10.0,
    d_s=5.0,
)

FIXTURES: dict[str, tuple[P.PacketKind, P.Packet]] = {
    "detection": (
        P.PacketKind.DETECTION,
        P.DetectionPacket(
            t_cap=123456,
            frame_seq=7,
            boxes=(
                P.Box(x=940.25, y=580.5, w=40.0, h=38.5, conf=0.91),
                P.Box(x=12.0, y=1100.0, w=6.5, h=6.0, conf=0.33),
            ),
        ),
    ),
    "detection_empty": (
        P.PacketKind.DETECTION,
        P.DetectionPacket(t_cap=123473, frame_seq=8, boxes=()),
    ),
    "track": (
        P.PacketKind.TRACK,
        P.TrackPacket(
            t_cap=123456,
            track_id=1_000_001,
            state=P.TrackState.CONFIRMED,
            u=960.125,
            v_px=599.75,
            du=-12.5,
            dv=3.0,
            w=40.0,
            h=38.5,
            hits=6,
            misses=0,
            age_frames=9,
        ),
    ),
    "mission_state": (
        P.PacketKind.MISSION_STATE,
        P.MissionStatePacket(
            t=124000,
            mission_state=P.MissionState.ENGAGED,
            engaged_track_id=1_000_001,
            trial=TRIAL,
            events=(
                P.Event(t=123990, name="cmd:approve_engage:accepted"),
                P.Event(t=123990, name="engaged:1000001"),
                P.Event(t=123990, name="transition:ACQUIRING->ENGAGED"),
            ),
        ),
    ),
    "mission_state_null_track": (
        P.PacketKind.MISSION_STATE,
        P.MissionStatePacket(
            t=100,
            mission_state=P.MissionState.PRIMED,
            engaged_track_id=None,
            trial=TRIAL,
            events=(),
        ),
    ),
    "fc_link_health": (
        P.PacketKind.FC_LINK_HEALTH,
        P.FcLinkHealthPacket(
            t=125000,
            attitude_age_ms=14,
            fc_link_up=True,
            rc_seen=True,
            gate_state=P.GateState.LOCKED,
            last_setpoint_t=None,
        ),
    ),
    "command_prime": (
        P.PacketKind.COMMAND,
        P.CommandPacket(
            cmd_id="ui-0001",
            token="fixture-token-not-a-secret",
            command=P.CommandName.PRIME,
            params=P.PrimeParams().to_obj(),
        ),
    ),
    "command_abort": (
        P.PacketKind.COMMAND,
        P.CommandPacket(
            cmd_id="ui-0002",
            token="fixture-token-not-a-secret",
            command=P.CommandName.ABORT,
        ),
    ),
    "ack": (
        P.PacketKind.ACK,
        P.AckPacket(cmd_id="ui-0001", result=P.AckResult.ACCEPTED),
    ),
}


def _golden_bytes(name: str, data: bytes) -> bytes:
    path = GOLDEN / f"{name}.json"
    if REGENERATE:
        GOLDEN.mkdir(exist_ok=True)
        path.write_bytes(data)
    return path.read_bytes()


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_p_golden_bytes_and_round_trip(name: str) -> None:
    """[C5], [P1]-[P5]: canonical bytes are pinned; decode(golden) is the
    fixture packet; re-encoding the decoded packet reproduces the bytes."""
    kind, packet = FIXTURES[name]
    data = P.encode(packet)
    golden = _golden_bytes(name, data)
    assert data == golden
    decoded = P.decode(kind, golden)
    assert decoded == packet
    assert P.encode(decoded) == golden


def test_p_golden_set_is_complete() -> None:
    """Every packet kind of contract §2 has at least one golden fixture, and
    no stale golden file lingers without a fixture."""
    kinds = {kind for kind, _ in FIXTURES.values()}
    assert kinds == set(P.PacketKind)
    on_disk = {p.stem for p in GOLDEN.glob("*.json")}
    assert on_disk == set(FIXTURES)


def test_p_canonical_form_is_ascii_sorted_compact() -> None:
    """[C5]: ASCII, sorted keys, no whitespace (checked on the golden bytes)."""
    for name in FIXTURES:
        raw = (GOLDEN / f"{name}.json").read_bytes()
        raw.decode("ascii")
        assert b" " not in raw and b"\n" not in raw
        obj = json.loads(raw)
        assert list(obj) == sorted(obj)


def _obj(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_bytes())


def _decode_obj(kind: P.PacketKind, obj: dict) -> P.Packet:
    return P.decode(kind, json.dumps(obj).encode())


def test_p_unknown_fields_are_ignored_at_every_level() -> None:
    """[C7]: receivers ignore unknown fields, top level and nested."""
    obj = _obj("detection")
    obj["future_field"] = {"anything": [1, 2]}
    obj["boxes"][0]["class_id"] = 3
    assert _decode_obj(P.PacketKind.DETECTION, obj) == FIXTURES["detection"][1]
    obj = _obj("mission_state")
    obj["trial"]["new_param"] = 1.0
    obj["events"][0]["detail"] = "x"
    assert _decode_obj(P.PacketKind.MISSION_STATE, obj) == FIXTURES["mission_state"][1]


@pytest.mark.parametrize("bad_v", [2, 0, "1", 1.0, True, None])
def test_p_version_other_than_1_is_rejected(bad_v: object) -> None:
    """[C7]: a packet whose v is not the integer 1 is rejected, never coerced."""
    obj = _obj("track")
    obj["v"] = bad_v
    with pytest.raises(P.PacketError):
        _decode_obj(P.PacketKind.TRACK, obj)


def test_p_version_2_raises_unsupported_version() -> None:
    """[C7]: a breaking change bumps v; this receiver names it as such."""
    obj = _obj("ack")
    obj["v"] = 2
    with pytest.raises(P.UnsupportedVersion):
        _decode_obj(P.PacketKind.ACK, obj)


def test_p_missing_v_is_rejected() -> None:
    obj = _obj("ack")
    del obj["v"]
    with pytest.raises(P.PacketError):
        _decode_obj(P.PacketKind.ACK, obj)


@pytest.mark.parametrize(
    ("name", "kind", "path", "value"),
    [
        # [C6] int fields take only JSON integers.
        ("detection", P.PacketKind.DETECTION, ("t_cap",), 123456.0),
        ("detection", P.PacketKind.DETECTION, ("frame_seq",), True),
        ("track", P.PacketKind.TRACK, ("hits",), "6"),
        # [C6] float fields take numbers, never booleans or strings.
        ("track", P.PacketKind.TRACK, ("u",), True),
        ("detection", P.PacketKind.DETECTION, ("boxes", 0, "conf"), "0.9"),
        # [C6] bool fields take only true/false.
        ("fc_link_health", P.PacketKind.FC_LINK_HEALTH, ("fc_link_up",), 1),
        # [C6] enum fields take only the listed values.
        ("track", P.PacketKind.TRACK, ("state",), "deleted"),
        ("mission_state", P.PacketKind.MISSION_STATE, ("mission_state",), "IDLE"),
        ("fc_link_health", P.PacketKind.FC_LINK_HEALTH, ("gate_state",), "open"),
        ("command_abort", P.PacketKind.COMMAND, ("command",), "set_velocity"),
        ("ack", P.PacketKind.ACK, ("result",), "ok"),
        # ranges
        ("detection", P.PacketKind.DETECTION, ("boxes", 0, "conf"), 1.5),
        ("detection", P.PacketKind.DETECTION, ("boxes", 0, "w"), 0.0),
        ("detection", P.PacketKind.DETECTION, ("t_cap",), -1),
        ("track", P.PacketKind.TRACK, ("track_id",), 0),
        ("track", P.PacketKind.TRACK, ("age_frames",), 0),
        ("track", P.PacketKind.TRACK, ("h",), -2.0),
        ("mission_state", P.PacketKind.MISSION_STATE, ("trial", "alpha"), 0.0),
        ("mission_state", P.PacketKind.MISSION_STATE, ("trial", "beta"), 0.6),
        ("mission_state", P.PacketKind.MISSION_STATE, ("engaged_track_id",), 0),
        ("mission_state", P.PacketKind.MISSION_STATE, ("events", 0, "name"), ""),
        ("fc_link_health", P.PacketKind.FC_LINK_HEALTH, ("attitude_age_ms",), -5),
        ("command_abort", P.PacketKind.COMMAND, ("cmd_id",), "has space"),
        ("command_abort", P.PacketKind.COMMAND, ("cmd_id",), "x" * 65),
        ("command_abort", P.PacketKind.COMMAND, ("token",), ""),
    ],
)
def test_p_type_and_range_violations_are_rejected(
    name: str, kind: P.PacketKind, path: tuple, value: object
) -> None:
    """[C6], [C7], [P1]-[P5]: wrong type or out-of-range value rejects the packet."""
    obj = _obj(name)
    node = obj
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(P.PacketError):
        _decode_obj(kind, obj)


@pytest.mark.parametrize(
    ("name", "kind", "field"),
    [
        ("detection", P.PacketKind.DETECTION, "boxes"),
        ("track", P.PacketKind.TRACK, "misses"),
        ("mission_state", P.PacketKind.MISSION_STATE, "engaged_track_id"),
        ("mission_state", P.PacketKind.MISSION_STATE, "trial"),
        ("fc_link_health", P.PacketKind.FC_LINK_HEALTH, "last_setpoint_t"),
        ("command_prime", P.PacketKind.COMMAND, "params"),
        ("ack", P.PacketKind.ACK, "result"),
    ],
)
def test_p_missing_required_field_is_rejected(name: str, kind: P.PacketKind, field: str) -> None:
    """[C7]: required fields (nullable ones included) must be present."""
    obj = _obj(name)
    del obj[field]
    with pytest.raises(P.PacketError):
        _decode_obj(kind, obj)


def test_p_float_fields_accept_json_integers() -> None:
    """[C6]: a C sender may write 940 for 940.0; the value is the same."""
    obj = _obj("detection")
    obj["boxes"][0]["w"] = 40
    pkt = _decode_obj(P.PacketKind.DETECTION, obj)
    assert pkt == FIXTURES["detection"][1]
    assert isinstance(pkt.boxes[0].w, float)


def test_p_non_finite_numbers_are_rejected_both_ways() -> None:
    """[C5]: no NaN or Infinity on the wire, in either direction."""
    raw = (GOLDEN / "track.json").read_bytes().replace(b'"du":-12.5', b'"du":NaN')
    with pytest.raises(P.PacketError):
        P.decode(P.PacketKind.TRACK, raw)
    inf_box = P.Box(x=float("inf"), y=0.0, w=1.0, h=1.0, conf=1.0)
    with pytest.raises(P.PacketError):
        P.encode(P.DetectionPacket(t_cap=1, frame_seq=1, boxes=(inf_box,)))


def test_p_datagram_size_ceiling() -> None:
    """[C9]: the encoder refuses, and the decoder rejects, > 65507 B."""
    boxes = tuple(P.Box(x=1.0, y=1.0, w=1.0, h=1.0, conf=0.5) for _ in range(2000))
    with pytest.raises(P.PacketError):
        P.encode(P.DetectionPacket(t_cap=1, frame_seq=1, boxes=boxes))
    with pytest.raises(P.PacketError):
        P.decode(P.PacketKind.ACK, b" " * (P.MAX_DATAGRAM_BYTES + 1))


def test_p_not_json_is_rejected() -> None:
    for raw in (b"\xff\xfe", b"{", b"[]", b"null"):
        with pytest.raises(P.PacketError):
            P.decode(P.PacketKind.ACK, raw)


def test_p_prime_requires_params_and_other_commands_ignore_them() -> None:
    """[P5]: params is required for prime and ignored for the other three."""
    obj = _obj("command_abort")
    obj["command"] = "prime"
    with pytest.raises(P.PacketError):
        _decode_obj(P.PacketKind.COMMAND, obj)
    obj = _obj("command_abort")
    obj["params"] = {"v_max": 99}
    assert _decode_obj(P.PacketKind.COMMAND, obj) == FIXTURES["command_abort"][1]


def test_p_salvage_cmd_id_from_malformed_command() -> None:
    """[P5b]: an undecodable command is acked rejected_malformed when its
    cmd_id is readable, so the sender learns the outcome."""
    obj = _obj("command_abort")
    obj["command"] = "fly_somewhere"
    raw = json.dumps(obj).encode()
    with pytest.raises(P.PacketError):
        P.decode(P.PacketKind.COMMAND, raw)
    assert P.salvage_cmd_id(raw) == "ui-0002"
    assert P.salvage_cmd_id(b"not json") is None
    assert P.salvage_cmd_id(b'{"cmd_id": "bad id"}') is None


def test_p_prime_params_defaults_are_contract_section_9() -> None:
    """[P5] with contract §9: the form defaults are the Provisional table."""
    p = P.PrimeParams()
    assert p.to_obj() == {
        "trial_type": "touch",
        "v_max": 2.5,
        "alpha": 0.4,
        "beta": 0.1,
        "k": 5,
        "pass_budget": 1,
        "search_alt": 10.0,
        "d_s": 5.0,
        "target_width_m": 1.0,
        "engage_preauthorized": False,
        "flight_time_cap_s": 180.0,
        "battery_floor_pct": 30.0,
        "geofence_radius_m": 60.0,
    }
    assert P.PrimeParams.from_obj(p.to_obj()) == p


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("trial_type", "chase"),
        ("v_max", 0.0),
        ("alpha", 1.01),
        ("beta", 0.0),
        ("k", 0),
        ("k", 5.0),
        ("pass_budget", 2),
        ("search_alt", 0.0),
        ("search_alt", 120.5),
        ("d_s", -1.0),
        ("target_width_m", 0.0),
        ("engage_preauthorized", 1),
        ("flight_time_cap_s", 0.0),
        ("battery_floor_pct", 100.5),
        ("geofence_radius_m", 0.0),
    ],
)
def test_p_prime_params_out_of_range_rejected(field: str, bad: object) -> None:
    """[P5]: an invalid set raises InvalidParams (acked rejected_params)."""
    obj = P.PrimeParams().to_obj()
    obj[field] = bad
    with pytest.raises(P.InvalidParams):
        P.PrimeParams.from_obj(obj)


def test_p_prime_params_missing_field_rejected() -> None:
    obj = P.PrimeParams().to_obj()
    del obj["geofence_radius_m"]
    with pytest.raises(P.InvalidParams):
        P.PrimeParams.from_obj(obj)


def test_p_v_max_hard_limit_discriminates() -> None:
    """[P5] load-bearing constant (doctrine rule 4): exactly v_max_hard passes,
    the next representable value above it fails."""
    import math

    obj = P.PrimeParams().to_obj()
    obj["v_max"] = P.V_MAX_HARD_MPS
    assert P.PrimeParams.from_obj(obj).v_max == 5.0
    obj["v_max"] = math.nextafter(P.V_MAX_HARD_MPS, math.inf)
    with pytest.raises(P.InvalidParams):
        P.PrimeParams.from_obj(obj)


def test_p_trial_echo_matches_prime() -> None:
    """[P3]: the echo carries exactly the eight trial fields of the prime."""
    echo = P.PrimeParams(trial_type=P.TrialType.STANDOFF, d_s=7.0).echo()
    assert echo == P.TrialEcho(
        trial_type=P.TrialType.STANDOFF,
        v_max=2.5,
        alpha=0.4,
        beta=0.1,
        k=5,
        pass_budget=1,
        search_alt=10.0,
        d_s=7.0,
    )
