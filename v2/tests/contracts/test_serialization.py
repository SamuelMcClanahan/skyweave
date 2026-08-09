"""T5: msgpack round trip, byte stability, unknown-field tolerance."""

from __future__ import annotations

import msgpack

from skyweave2.contracts import (
    ClockDomain,
    LocalizationResult,
    Observation2D,
    ResultStatus,
    SystematicSource,
    Track,
    TrackState,
    pack,
    unpack,
)


def _result() -> LocalizationResult:
    return LocalizationResult(
        ts_ns=1_400_000_000,
        clock_domain=ClockDomain.SYNTHETIC,
        position=(10.0, 200.0, 55.0),
        covariance=(0.04, 0.0, 0.01, 0.0, 0.09, 0.0, 0.01, 0.0, 4.0),
        systematic_bound=(0.1, 0.1, 0.4),
        systematic_sources=[SystematicSource.CALIBRATION, SystematicSource.TARGET_REFERENCE],
        residual_px_rms=0.6,
        supporting_camera_ids=[0, 1, 2],
        triangulation_angle_deg=11.5,
        condition=35.0,
        status=ResultStatus.CONFIRMED,
        obs_ids=["0:s:42:0", "1:s:42:0", "2:s:42:1"],
    )


def test_observation_round_trip(observation: Observation2D) -> None:
    assert unpack(pack(observation), Observation2D) == observation


def test_localization_round_trip() -> None:
    assert unpack(pack(_result()), LocalizationResult) == _result()


def test_track_round_trip() -> None:
    track = Track(
        track_id=1,
        session_uuid="jetson-session-1",
        state=(10.0, 200.0, 55.0, 1.0, -2.0, 0.0),
        covariance=tuple([0.0] * 36),
        status=TrackState.CONFIRMED,
        created_ts_ns=0,
        last_update_ts_ns=1_400_000_000,
        update_count=12,
        miss_count=0,
    )
    assert unpack(pack(track), Track) == track


def test_pack_is_byte_stable(observation: Observation2D) -> None:
    assert pack(observation) == pack(observation.model_copy(deep=True))


def test_unknown_fields_are_ignored(observation: Observation2D) -> None:
    payload = msgpack.unpackb(pack(observation), raw=False)
    payload["field_from_the_future"] = 123
    payload["envelope"]["another_new_field"] = "x"
    decoded = Observation2D.model_validate(payload)
    assert decoded == observation
