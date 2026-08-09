"""T4: clock domains, session identity, reboot handling, capture immutability."""

from __future__ import annotations

import pydantic
import pytest

from skyweave2.contracts import ClockMapping, FrameEnvelope, SessionEvent, SessionRegistry


def _env(envelope: FrameEnvelope, **updates: object) -> FrameEnvelope:
    return envelope.model_copy(update=updates)


def test_capture_fields_are_immutable(envelope: FrameEnvelope) -> None:
    with pytest.raises(pydantic.ValidationError):
        envelope.capture_ts_ns = 0  # type: ignore[misc]


def test_in_order_frames(envelope: FrameEnvelope) -> None:
    registry = SessionRegistry()
    assert registry.classify(envelope) is SessionEvent.IN_ORDER
    assert registry.classify(_env(envelope, frame_seq=43)) is SessionEvent.IN_ORDER


def test_reboot_with_sequence_reset_is_a_new_session(envelope: FrameEnvelope) -> None:
    registry = SessionRegistry()
    registry.classify(envelope)
    rebooted = _env(envelope, session_uuid="99999999-0000-0000-0000-000000000000", frame_seq=0)
    assert registry.classify(rebooted) is SessionEvent.NEW_SESSION
    # And the new session continues in order afterwards.
    assert registry.classify(_env(rebooted, frame_seq=1)) is SessionEvent.IN_ORDER


def test_same_session_sequence_regression_is_flagged(envelope: FrameEnvelope) -> None:
    registry = SessionRegistry()
    registry.classify(envelope)
    stale = _env(envelope, frame_seq=41)
    assert registry.classify(stale) is SessionEvent.STALE_OR_DUPLICATE
    duplicate = _env(envelope, frame_seq=42)
    assert registry.classify(duplicate) is SessionEvent.STALE_OR_DUPLICATE


def test_cameras_are_tracked_independently(envelope: FrameEnvelope) -> None:
    registry = SessionRegistry()
    registry.classify(envelope)
    other_camera = _env(envelope, camera_id=9, frame_seq=0)
    assert registry.classify(other_camera) is SessionEvent.IN_ORDER


def test_clock_mapping_preserves_source_value(envelope: FrameEnvelope) -> None:
    mapping = ClockMapping(offset_ns=2_000_000, drift_ppm=10.0, uncertainty_ms=0.5)
    mapped = mapping.map_ns(envelope.capture_ts_ns)
    assert mapped == 1_400_000_000 + 2_000_000 + 14_000
    # The envelope still carries the original capture time: mapping never mutates.
    assert envelope.capture_ts_ns == 1_400_000_000
