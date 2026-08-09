"""T7: optional evidence is droppable; the measurement always survives."""

from __future__ import annotations

from skyweave2.contracts import Observation2D, pack, unpack


def test_drop_evidence_keeps_every_measurement_field(observation: Observation2D) -> None:
    dropped = observation.drop_evidence()
    assert dropped.evidence_ref is None
    kept = observation.model_dump(exclude={"evidence_ref"})
    assert dropped.model_dump(exclude={"evidence_ref"}) == kept


def test_observation_valid_without_evidence(observation: Observation2D) -> None:
    dropped = observation.drop_evidence()
    decoded = unpack(pack(dropped), Observation2D)
    assert decoded.u == observation.u
    assert decoded.v == observation.v
    assert decoded.envelope.capture_ts_ns == observation.envelope.capture_ts_ns
    assert decoded.cov_uu == observation.cov_uu


def test_geometry_inputs_do_not_reference_evidence(observation: Observation2D) -> None:
    # The geometry-facing fields are exactly these; none depend on the patch.
    geometry_fields = {"u", "v", "cov_uu", "cov_uv", "cov_vv"}
    for field in geometry_fields:
        assert getattr(observation.drop_evidence(), field) == getattr(observation, field)
