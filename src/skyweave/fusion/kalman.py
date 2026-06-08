from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Literal

import numpy as np
from filterpy.kalman import KalmanFilter

from skyweave.config import KalmanConfig
from skyweave.messages import Measurement3D, Track

TrackStatus = Literal["candidate", "active", "coasting"]


class TrackManager:
    def __init__(self, config: KalmanConfig) -> None:
        self.config = config
        self._next_track_id = 1
        self._track_id: int | None = None
        self._kf: KalmanFilter | None = None
        self._created_ts_ns: int | None = None
        self._last_ts_ns: int | None = None
        self._last_measurement_ts_ns: int | None = None
        self._update_count = 0
        self._miss_count = 0
        self._trail: deque[tuple[float, float, float, int]] = deque(maxlen=200)

    def update(self, measurement: Measurement3D | Sequence[Measurement3D] | None, ts_ns: int) -> Track | None:
        candidates = _measurement_candidates(measurement)
        if self._kf is None:
            if not candidates:
                return None
            measurement = max(candidates, key=lambda item: item.score)
            self._initialize(measurement)
            return self._to_track("candidate")

        if self._coast_expired(ts_ns):
            self._retire()
            if not candidates:
                return None
            measurement = max(candidates, key=lambda item: item.score)
            self._initialize(measurement)
            return self._to_track("candidate")

        self._predict(_elapsed_seconds(self._last_ts_ns, ts_ns))
        self._last_ts_ns = ts_ns

        measurement = self._select_measurement(candidates)
        if measurement is None:
            self._miss_count += 1
            return self._to_track("coasting")

        self._correct(measurement)
        self._update_count += 1
        self._miss_count = 0
        self._last_measurement_ts_ns = measurement.ts_ns
        self._append_trail_point()
        return self._to_track("active" if self._update_count >= 3 else "candidate")

    def _initialize(self, measurement: Measurement3D) -> None:
        kf = KalmanFilter(dim_x=6, dim_z=3)
        kf.x = _initial_state(measurement)
        kf.P = _initial_covariance(self.config)
        kf.H = _measurement_matrix()
        kf.F = _transition_matrix(0.0)
        kf.Q = _process_noise_matrix(0.0, self.config.sigma_accel_mps2)
        kf.R = _measurement_noise_matrix(measurement, self.config.measurement_var_scale)

        self._track_id = self._next_track_id
        self._next_track_id += 1
        self._kf = kf
        self._created_ts_ns = measurement.ts_ns
        self._last_ts_ns = measurement.ts_ns
        self._last_measurement_ts_ns = measurement.ts_ns
        self._update_count = 1
        self._miss_count = 0
        self._trail = deque(maxlen=200)
        self._append_trail_point()

    def _predict(self, dt: float) -> None:
        assert self._kf is not None
        _set_transition_matrix(self._kf.F, dt)
        _set_process_noise_matrix(self._kf.Q, dt, self.config.sigma_accel_mps2)
        self._kf.predict()

    def _correct(self, measurement: Measurement3D) -> None:
        assert self._kf is not None
        self._kf.R = _measurement_noise_matrix(measurement, self.config.measurement_var_scale)
        self._kf.update(_measurement_vector(measurement))
        self._last_ts_ns = measurement.ts_ns

    def _select_measurement(self, candidates: Sequence[Measurement3D]) -> Measurement3D | None:
        if not candidates:
            return None
        if self._kf is None:
            return max(candidates, key=lambda item: item.score)

        scored: list[tuple[float, float, Measurement3D]] = []
        for measurement in candidates:
            distance_squared = _mahalanobis_squared(self._kf, measurement, self.config.measurement_var_scale)
            if not np.isfinite(distance_squared):
                continue
            gate = float(self.config.gate_mahalanobis_squared)
            if gate > 0.0 and distance_squared > gate:
                continue
            scored.append((distance_squared, -float(measurement.score), measurement))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1]))
        return scored[0][2]

    def _append_trail_point(self) -> None:
        state = self._state()
        self._trail.append((float(state[0]), float(state[1]), float(state[2]), int(self._last_ts_ns or 0)))

    def _to_track(self, status: TrackStatus) -> Track:
        assert self._kf is not None
        assert self._track_id is not None
        return Track.model_construct(
            id=self._track_id,
            state=[float(x) for x in self._state()],
            covariance=np.asarray(self._kf.P, dtype=np.float64).tolist(),
            status=status,
            created_ts_ns=int(self._created_ts_ns or self._last_ts_ns or 0),
            last_update_ts_ns=int(self._last_ts_ns or 0),
            update_count=self._update_count,
            miss_count=self._miss_count,
            trail=list(self._trail),
        )

    def _state(self) -> np.ndarray:
        assert self._kf is not None
        return np.asarray(self._kf.x, dtype=np.float64).reshape(6)

    def _coast_expired(self, ts_ns: int) -> bool:
        if self._last_measurement_ts_ns is None:
            return False
        return _elapsed_seconds(self._last_measurement_ts_ns, ts_ns) > self.config.coast_seconds

    def _retire(self) -> None:
        self._track_id = None
        self._kf = None
        self._created_ts_ns = None
        self._last_ts_ns = None
        self._last_measurement_ts_ns = None
        self._update_count = 0
        self._miss_count = 0
        self._trail = deque(maxlen=200)


def _initial_state(measurement: Measurement3D) -> np.ndarray:
    px, py, pz = measurement.position
    return np.array(
        [
            [px],
            [py],
            [pz],
            [0.0],
            [0.0],
            [0.0],
        ],
        dtype=np.float64,
    )


def _initial_covariance(config: KalmanConfig) -> np.ndarray:
    p = config.initial_position_var
    v = config.initial_velocity_var
    return np.array(
        [
            [p, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, p, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, p, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, v, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, v, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, v],
        ],
        dtype=np.float64,
    )


def _measurement_matrix() -> np.ndarray:
    # State is [px, py, pz, vx, vy, vz]. Measurement is [px, py, pz].
    return np.array(
        [
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        ],
        dtype=np.float64,
    )


def _transition_matrix(dt: float) -> np.ndarray:
    return np.array(
        [
            [1.0, 0.0, 0.0, dt, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0, dt, 0.0],
            [0.0, 0.0, 1.0, 0.0, 0.0, dt],
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def _set_transition_matrix(matrix: np.ndarray, dt: float) -> None:
    matrix.fill(0.0)
    matrix[0, 0] = 1.0
    matrix[1, 1] = 1.0
    matrix[2, 2] = 1.0
    matrix[3, 3] = 1.0
    matrix[4, 4] = 1.0
    matrix[5, 5] = 1.0
    matrix[0, 3] = dt
    matrix[1, 4] = dt
    matrix[2, 5] = dt


def _process_noise_matrix(dt: float, sigma_accel_mps2: float) -> np.ndarray:
    q = sigma_accel_mps2**2
    pos = 0.25 * dt**4 * q
    cross = 0.5 * dt**3 * q
    vel = dt**2 * q

    return np.array(
        [
            [pos, 0.0, 0.0, cross, 0.0, 0.0],
            [0.0, pos, 0.0, 0.0, cross, 0.0],
            [0.0, 0.0, pos, 0.0, 0.0, cross],
            [cross, 0.0, 0.0, vel, 0.0, 0.0],
            [0.0, cross, 0.0, 0.0, vel, 0.0],
            [0.0, 0.0, cross, 0.0, 0.0, vel],
        ],
        dtype=np.float64,
    )


def _set_process_noise_matrix(matrix: np.ndarray, dt: float, sigma_accel_mps2: float) -> None:
    q = sigma_accel_mps2**2
    pos = 0.25 * dt**4 * q
    cross = 0.5 * dt**3 * q
    vel = dt**2 * q

    matrix.fill(0.0)
    matrix[0, 0] = pos
    matrix[1, 1] = pos
    matrix[2, 2] = pos
    matrix[0, 3] = cross
    matrix[1, 4] = cross
    matrix[2, 5] = cross
    matrix[3, 0] = cross
    matrix[4, 1] = cross
    matrix[5, 2] = cross
    matrix[3, 3] = vel
    matrix[4, 4] = vel
    matrix[5, 5] = vel


def _measurement_vector(measurement: Measurement3D) -> np.ndarray:
    mx, my, mz = measurement.position
    return np.array(
        [
            [mx],
            [my],
            [mz],
        ],
        dtype=np.float64,
    )


def _measurement_noise_matrix(measurement: Measurement3D, scale: float) -> np.ndarray:
    return np.asarray(measurement.covariance, dtype=np.float64) * scale


def _measurement_candidates(measurement: Measurement3D | Sequence[Measurement3D] | None) -> list[Measurement3D]:
    if measurement is None:
        return []
    if isinstance(measurement, Measurement3D):
        return [measurement]
    return list(measurement)


def _mahalanobis_squared(kf: KalmanFilter, measurement: Measurement3D, measurement_var_scale: float) -> float:
    h = np.asarray(kf.H, dtype=np.float64)
    state = np.asarray(kf.x, dtype=np.float64).reshape(6, 1)
    innovation = _measurement_vector(measurement) - h @ state
    innovation_covariance = h @ np.asarray(kf.P, dtype=np.float64) @ h.T
    innovation_covariance += _measurement_noise_matrix(measurement, measurement_var_scale)
    try:
        solved = np.linalg.solve(innovation_covariance, innovation)
    except np.linalg.LinAlgError:
        solved = np.linalg.pinv(innovation_covariance) @ innovation
    return float((innovation.T @ solved).item())


def _elapsed_seconds(last_ts_ns: int | None, ts_ns: int) -> float:
    if last_ts_ns is None:
        return 0.0
    return max((ts_ns - last_ts_ns) / 1_000_000_000.0, 0.0)
