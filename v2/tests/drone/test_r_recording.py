"""R series: whole-flight recording (DRONE_CONTRACTS_D0.md §3).

[R3] replay determinism is enforced at the companion-core level, where the
recording is fed back through the real mission and guidance code; this file
pins the format itself.
"""

from __future__ import annotations

import io
import json

import pytest

from skyweave2.drone import packets as P
from skyweave2.drone.recording import (
    FORMAT,
    REDACTED_TOKEN,
    Recorder,
    RecordingError,
    Stream,
    parse_record,
    read_records,
)

FIXTURE_TOKEN = "fixture-token-not-a-secret"


def _sample_recording() -> str:
    buf = io.StringIO()
    rec = Recorder(buf)
    rec.meta(0, {"coast_cap": 20, "camera": {"f_px": 1000.0}})
    rec.packet(10, P.DetectionPacket(t_cap=9, frame_seq=1, boxes=()))
    rec.packet(
        11,
        P.TrackPacket(
            t_cap=9,
            track_id=5,
            state=P.TrackState.TENTATIVE,
            u=1.0,
            v_px=2.0,
            du=0.0,
            dv=0.0,
            w=3.0,
            h=4.0,
            hits=1,
            misses=0,
            age_frames=1,
        ),
    )
    rec.command(
        12,
        P.CommandPacket(cmd_id="c1", token=FIXTURE_TOKEN, command=P.CommandName.ABORT),
        auth_ok=True,
    )
    rec.packet(13, P.AckPacket(cmd_id="c1", result=P.AckResult.ACCEPTED))
    rec.mavlink(14, "rx", b"\xfd\x09\x00\x00\x01")
    rec.mavlink(15, "tx", b"\xfd\x00")
    rec.tick(16)
    rec.ground_hb(17)
    rec.packet(
        18,
        P.FcLinkHealthPacket(
            t=18,
            attitude_age_ms=None,
            fc_link_up=False,
            rc_seen=False,
            gate_state=P.GateState.LOCKED,
            last_setpoint_t=None,
        ),
    )
    return buf.getvalue()


def test_r_round_trip_every_stream() -> None:
    """[R2]: every record type written by the Recorder reads back in order."""
    text = _sample_recording()
    records = list(read_records(text.splitlines()))
    assert [r.stream for r in records] == [
        Stream.META,
        Stream.DETECTION,
        Stream.TRACK,
        Stream.COMMAND,
        Stream.ACK,
        Stream.MAVLINK,
        Stream.MAVLINK,
        Stream.TICK,
        Stream.GROUND_HB,
        Stream.FC_LINK_HEALTH,
    ]
    assert [r.t_rx for r in records] == [0, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    assert records[0].config == {"coast_cap": 20, "camera": {"f_px": 1000.0}}
    assert records[2].packet.track_id == 5
    assert records[5].direction == "rx" and records[5].raw == b"\xfd\x09\x00\x00\x01"
    assert records[6].direction == "tx" and records[6].raw == b"\xfd\x00"


def test_r_token_is_never_written() -> None:
    """[P5c], [R2]: the secret never reaches a recording; the auth result does."""
    text = _sample_recording()
    assert FIXTURE_TOKEN not in text
    cmd = [r for r in read_records(text.splitlines()) if r.stream is Stream.COMMAND][0]
    assert cmd.auth_ok is True
    assert cmd.packet.token == REDACTED_TOKEN
    assert cmd.packet.command is P.CommandName.ABORT


def test_r_commands_cannot_bypass_redaction() -> None:
    """[R2]: Recorder.packet refuses a command (it would keep the token)."""
    rec = Recorder(io.StringIO())
    with pytest.raises(TypeError):
        rec.packet(1, P.CommandPacket(cmd_id="c", token=FIXTURE_TOKEN, command=P.CommandName.ABORT))


def test_r_lines_are_canonical_json() -> None:
    """[R1], [C5]: one canonical object per line."""
    for line in _sample_recording().splitlines():
        assert " " not in line
        assert line.startswith("{") and line.endswith("}")


def test_r_no_frame_record_type_exists() -> None:
    """[R1]: packets only. The format has no stream that can carry pixels."""
    assert {s.value for s in Stream} == {
        "meta",
        "detection",
        "track",
        "mission_state",
        "fc_link_health",
        "command",
        "ack",
        "mavlink",
        "tick",
        "ground_hb",
    }
    with pytest.raises(RecordingError):
        parse_record('{"t_rx":1,"stream":"frame","pixels":"AAAA"}')


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '{"stream":"tick"}',
        '{"t_rx":-1,"stream":"tick"}',
        '{"t_rx":true,"stream":"tick"}',
        '{"t_rx":1,"stream":"meta","format":"other","format_v":1,"config":{}}',
        '{"t_rx":1,"stream":"mavlink","dir":"up","raw":""}',
        '{"t_rx":1,"stream":"mavlink","dir":"rx","raw":"***"}',
        '{"t_rx":1,"stream":"command","pkt":{"v":1,"cmd_id":"a","command":"abort"}}',
        '{"t_rx":1,"stream":"ack","pkt":{"v":2,"cmd_id":"a","result":"accepted"}}',
        # [R2], [P5c]: a command record that carries a token is malformed
        '{"t_rx":1,"stream":"command","pkt":{"v":1,"cmd_id":"a","command":"abort",'
        '"token":"x"},"auth_ok":true}',
        '{"t_rx":1,"stream":"command","pkt":[["v",1],["cmd_id","a"]],"auth_ok":true}',
        # [C5]: non-finite numbers are refused on read as on the wire
        '{"t_rx":1,"stream":"meta","format":"skyweave-drone-rec","format_v":1,'
        '"config":{"x":1e999}}',
        "[" * 50_000,
    ],
)
def test_r_malformed_records_are_rejected(line: str) -> None:
    """[R2]: a line outside the format raises, never yields a guessed record."""
    with pytest.raises((RecordingError, P.PacketError)):
        parse_record(line)


def test_r_read_records_reports_line_number(tmp_path) -> None:
    """[R2]: a line outside the format stops the read and names its line."""
    sample = _sample_recording()
    bad_line = len(sample.splitlines()) + 1
    path = tmp_path / "rec.jsonl"
    path.write_text(sample + "garbage\n", encoding="ascii")
    with pytest.raises(RecordingError, match=f"line {bad_line}"):
        list(read_records(path))


def test_r_meta_line_carries_format_and_version() -> None:
    """[R2]: the first line is the meta record, with format
    skyweave-drone-rec and format_v 1, as the contract table states."""
    first = json.loads(_sample_recording().splitlines()[0])
    assert first["stream"] == "meta"
    assert first["format"] == FORMAT == "skyweave-drone-rec"
    assert first["format_v"] == 1
