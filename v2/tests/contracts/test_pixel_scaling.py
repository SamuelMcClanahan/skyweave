"""T2: resolution scaling law and pixel-center convention."""

from __future__ import annotations

import pytest

from skyweave2.contracts import FrameEnvelope, full_to_proc, proc_to_full


def test_center_of_proc_pixel_zero_maps_to_block_center() -> None:
    # scale 2: proc pixel 0 covers full pixels 0..1, whose center is 0.5.
    assert proc_to_full(0.0, 2.0) == pytest.approx(0.5)


def test_scale_one_is_identity() -> None:
    for coord in (-0.5, 0.0, 1.25, 639.0):
        assert proc_to_full(coord, 1.0) == pytest.approx(coord)


def test_round_trip_exact() -> None:
    for scale in (1.5, 2.0, 3.0):
        for coord in (-0.5, 0.0, 0.5, 100.25, 863.0):
            assert full_to_proc(proc_to_full(coord, scale), scale) == pytest.approx(coord)


def test_image_edges_map_to_edges() -> None:
    # Left edge of the image is -0.5 in both grids; right edge W-0.5 maps too.
    scale = 2304 / 1536
    assert proc_to_full(-0.5, scale) == pytest.approx(-0.5)
    assert proc_to_full(1536 - 0.5, scale) == pytest.approx(2304 - 0.5)


def test_envelope_declares_scales(envelope: FrameEnvelope) -> None:
    assert envelope.proc_scale_x == pytest.approx(1.5)
    assert envelope.proc_scale_y == pytest.approx(1.5)
