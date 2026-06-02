from skyweave.config import KalmanConfig
from skyweave.fusion.kalman import TrackManager
from skyweave.messages import Measurement3D


def _measurement(ts_ns: int, x: float) -> Measurement3D:
    return Measurement3D(
        ts_ns=ts_ns,
        source="voxel_peak",
        position=(x, 0.0, 1.0),
        covariance=[
            [0.01, 0.0, 0.0],
            [0.0, 0.01, 0.0],
            [0.0, 0.0, 0.01],
        ],
        score=1.0,
        supporting_camera_ids=[0, 1, 2],
    )


def test_filterpy_track_manager_updates_and_coasts() -> None:
    manager = TrackManager(KalmanConfig())

    first = manager.update(_measurement(0, 0.0), 0)
    assert first is not None
    assert first.status == "candidate"

    second = manager.update(_measurement(1_000_000_000, 1.0), 1_000_000_000)
    third = manager.update(_measurement(2_000_000_000, 2.0), 2_000_000_000)
    assert second is not None
    assert third is not None
    assert third.status == "active"
    assert third.state[0] > 1.5

    coasted = manager.update(None, 3_000_000_000)
    assert coasted is not None
    assert coasted.status == "coasting"
    assert coasted.state[0] > third.state[0]
