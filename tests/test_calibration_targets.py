from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_generator_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "generate_charuco_board.py"
    spec = importlib.util.spec_from_file_location("generate_charuco_board", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_board_spec_sizes_and_output_path() -> None:
    generator = _load_generator_module()
    spec = generator.BoardSpec(
        squares_x=10,
        squares_y=7,
        square_mm=40.0,
        marker_mm=30.0,
        dictionary="DICT_5X5_1000",
        dpi=300,
        margin_mm=10.0,
    )

    assert spec.board_width_mm == 400.0
    assert spec.board_height_mm == 280.0
    assert spec.total_width_mm == 420.0
    assert spec.total_height_mm == 300.0
    assert generator._mm_to_px(25.4, 300) == 300
    assert generator._default_output(spec).name == "charuco_10x7_40mm_30mm_DICT_5X5_1000.png"


def test_board_spec_rejects_marker_larger_than_square() -> None:
    generator = _load_generator_module()
    spec = generator.BoardSpec(
        squares_x=10,
        squares_y=7,
        square_mm=40.0,
        marker_mm=41.0,
        dictionary="DICT_5X5_1000",
        dpi=300,
        margin_mm=10.0,
    )

    with pytest.raises(SystemExit):
        generator._validate_spec(spec)
