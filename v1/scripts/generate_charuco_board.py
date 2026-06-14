#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class BoardSpec:
    squares_x: int
    squares_y: int
    square_mm: float
    marker_mm: float
    dictionary: str
    dpi: int
    margin_mm: float

    @property
    def board_width_mm(self) -> float:
        return self.squares_x * self.square_mm

    @property
    def board_height_mm(self) -> float:
        return self.squares_y * self.square_mm

    @property
    def total_width_mm(self) -> float:
        return self.board_width_mm + 2.0 * self.margin_mm

    @property
    def total_height_mm(self) -> float:
        return self.board_height_mm + 2.0 * self.margin_mm


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a printable ChArUco board PNG and YAML metadata.")
    parser.add_argument("--squares-x", type=int, default=10)
    parser.add_argument("--squares-y", type=int, default=7)
    parser.add_argument("--square-mm", type=float, default=40.0)
    parser.add_argument("--marker-mm", type=float, default=30.0)
    parser.add_argument("--dictionary", default="DICT_5X5_1000")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--margin-mm", type=float, default=10.0)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    spec = BoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_mm=args.square_mm,
        marker_mm=args.marker_mm,
        dictionary=args.dictionary,
        dpi=args.dpi,
        margin_mm=args.margin_mm,
    )
    _validate_spec(spec)
    output = Path(args.output) if args.output else _default_output(spec)
    render_charuco_board(spec, output)
    metadata = _metadata_path(output)
    write_metadata(spec, output, metadata)
    print(f"board_png={output}")
    print(f"metadata_yaml={metadata}")
    print(f"print_size_mm={spec.total_width_mm:.1f}x{spec.total_height_mm:.1f}")
    print("print_at_100_percent=true")
    print("measure_square_size_after_printing=true")
    return 0


def render_charuco_board(spec: BoardSpec, output: Path) -> None:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise SystemExit("OpenCV is required. Install with: python -m pip install -e '.[camera]'") from exc

    aruco = cv2.aruco
    dictionary_id = getattr(aruco, spec.dictionary, None)
    if dictionary_id is None:
        raise SystemExit(f"Unknown OpenCV ArUco dictionary: {spec.dictionary}")

    dictionary = aruco.getPredefinedDictionary(dictionary_id)
    square_m = spec.square_mm / 1000.0
    marker_m = spec.marker_mm / 1000.0
    board = _create_charuco_board(aruco, spec, square_m, marker_m, dictionary)

    board_px = (_mm_to_px(spec.board_width_mm, spec.dpi), _mm_to_px(spec.board_height_mm, spec.dpi))
    margin_px = _mm_to_px(spec.margin_mm, spec.dpi)
    image = _draw_board(aruco, board, board_px)
    if margin_px > 0:
        canvas = np.full((image.shape[0] + 2 * margin_px, image.shape[1] + 2 * margin_px), 255, dtype=image.dtype)
        canvas[margin_px : margin_px + image.shape[0], margin_px : margin_px + image.shape[1]] = image
        image = canvas

    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), image):
        raise SystemExit(f"Failed to write {output}")


def write_metadata(spec: BoardSpec, output: Path, metadata: Path) -> None:
    payload = {
        **asdict(spec),
        "board_width_mm": spec.board_width_mm,
        "board_height_mm": spec.board_height_mm,
        "total_width_mm": spec.total_width_mm,
        "total_height_mm": spec.total_height_mm,
        "board_png": str(output),
        "notes": [
            "Print at 100 percent scale.",
            "Mount flat on rigid backing.",
            "Measure the printed square size and use that measured value for calibration.",
        ],
    }
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _create_charuco_board(aruco, spec: BoardSpec, square_m: float, marker_m: float, dictionary):
    if hasattr(aruco, "CharucoBoard"):
        try:
            return aruco.CharucoBoard((spec.squares_x, spec.squares_y), square_m, marker_m, dictionary)
        except TypeError:
            pass
    if hasattr(aruco, "CharucoBoard_create"):
        return aruco.CharucoBoard_create(spec.squares_x, spec.squares_y, square_m, marker_m, dictionary)
    raise SystemExit("This OpenCV build does not include ChArUco board generation.")


def _draw_board(aruco, board, board_px: tuple[int, int]):
    if hasattr(board, "generateImage"):
        return board.generateImage(board_px, marginSize=0, borderBits=1)
    if hasattr(board, "draw"):
        return board.draw(board_px, marginSize=0, borderBits=1)
    if hasattr(aruco, "drawPlanarBoard"):
        return aruco.drawPlanarBoard(board, board_px, marginSize=0, borderBits=1)
    raise SystemExit("This OpenCV build does not include a ChArUco drawing API.")


def _validate_spec(spec: BoardSpec) -> None:
    if spec.squares_x < 3 or spec.squares_y < 3:
        raise SystemExit("ChArUco board must have at least 3 squares in each direction.")
    if spec.square_mm <= 0.0:
        raise SystemExit("square-mm must be positive.")
    if not 0.0 < spec.marker_mm < spec.square_mm:
        raise SystemExit("marker-mm must be positive and smaller than square-mm.")
    if spec.dpi <= 0:
        raise SystemExit("dpi must be positive.")
    if spec.margin_mm < 0.0:
        raise SystemExit("margin-mm cannot be negative.")


def _mm_to_px(mm: float, dpi: int) -> int:
    return max(1, int(round(mm / 25.4 * dpi)))


def _default_output(spec: BoardSpec) -> Path:
    square = f"{spec.square_mm:g}mm"
    marker = f"{spec.marker_mm:g}mm"
    return Path("data/calibration_targets") / f"charuco_{spec.squares_x}x{spec.squares_y}_{square}_{marker}_{spec.dictionary}.png"


def _metadata_path(output: Path) -> Path:
    return output.with_suffix(".yaml")


if __name__ == "__main__":
    raise SystemExit(main())
