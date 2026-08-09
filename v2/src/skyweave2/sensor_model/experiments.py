"""The Bayer round-trip experiment (D3 brief, stage B alone).

Question answered (SYNTHETIC_PIPELINE_DESIGN open question 1): does
mosaic+demosaic meaningfully bias the centroid of a 2-9 px blob, as a
function of blob size and sub-pixel phase?

Method: render a noiseless Gaussian blob of known FWHM at a known
sub-pixel position onto a flat background, run the §5.1 chain with every
stochastic stage disabled, with the mosaic stage ON and OFF, and compare
intensity-weighted centroids of the background-subtracted U8 output
against the injected truth. Fully deterministic — no RNG anywhere.

Usage (from ``v2/``):

    uv run python -m skyweave2.sensor_model.experiments --out docs/D3_SENSOR_NOTES.md

Every number produced is Modeled.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np

from skyweave2.sensor_model.config import SensorModelSpec, TruthCameraOptics
from skyweave2.sensor_model.pipeline import render_frame

_FWHM_TO_SIGMA = 1.0 / 2.354820045  # sigma = FWHM / (2 sqrt(2 ln 2))

BLOB_SIZES_PX = (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)
PHASES = (0.0, 0.25, 0.5, 0.75)  # applied to both axes


@dataclass(frozen=True)
class RoundTripRow:
    size_px: float
    phase: float
    bias_off_px: float  # |centroid - truth|, mosaic disabled
    bias_on_px: float  # |centroid - truth|, mosaic enabled
    added_bias_px: float  # bias_on - bias_off


def _experiment_spec(enable_mosaic: bool) -> SensorModelSpec:
    """Chain config with every stochastic/optical stage off except the ISP tail."""
    return SensorModelSpec(
        enable_vignetting=False,
        enable_psf=False,
        enable_distortion=False,
        enable_shot_noise=False,
        enable_dark=False,
        enable_prnu=False,
        enable_read_noise=False,
        enable_ae=False,
        enable_mosaic=enable_mosaic,
        # Scale chosen so background sits mid-range and the blob does not
        # clip: full_well maps near the ADC top at unit gain.
        electrons_per_radiance_second=1.0e6,
        exposure_s=0.003,
    )


def _blob_radiance(
    shape: tuple[int, int], center_uv: tuple[float, float], sigma_px: float
) -> np.ndarray:
    """Flat background plus a Gaussian blob (pixel-center convention)."""
    height, width = shape
    v, u = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    cu, cv = center_uv
    blob = np.exp(-0.5 * (((u - cu) / sigma_px) ** 2 + ((v - cv) / sigma_px) ** 2))
    background = 0.4
    peak = 1.2
    return background + peak * blob


def _measured_centroid(y_u8: np.ndarray, y_background_u8: np.ndarray) -> tuple[float, float]:
    """Intensity-weighted centroid of the background-subtracted response."""
    diff = y_u8.astype(np.float64) - y_background_u8.astype(np.float64)
    diff = np.clip(diff, 0.0, None)
    total = diff.sum()
    if total <= 0.0:
        return float("nan"), float("nan")
    height, width = diff.shape
    v, u = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    return float((diff * u).sum() / total), float((diff * v).sum() / total)


def _render_y(radiance: np.ndarray, spec: SensorModelSpec, camera: TruthCameraOptics) -> np.ndarray:
    y_u8, _ = render_frame(
        radiance, camera, spec, dataset_seed=0, camera_id=0, frame_seq=0
    )
    return y_u8


def run_round_trip(
    sizes_px: tuple[float, ...] = BLOB_SIZES_PX, phases: tuple[float, ...] = PHASES
) -> list[RoundTripRow]:
    side = 64
    camera = TruthCameraOptics(
        width=side, height=side, fx=1000.0, fy=1000.0, cx=(side - 1) / 2, cy=(side - 1) / 2
    )
    spec_on = _experiment_spec(enable_mosaic=True)
    spec_off = _experiment_spec(enable_mosaic=False)
    flat = np.full((side, side), 0.4, dtype=np.float64)
    background_on = _render_y(flat, spec_on, camera)
    background_off = _render_y(flat, spec_off, camera)

    rows: list[RoundTripRow] = []
    base = (side - 1) / 2.0  # integer-pixel-centered start; phase shifts off it
    for size in sizes_px:
        sigma = size * _FWHM_TO_SIGMA
        for phase in phases:
            truth = (base + phase, base + phase)
            radiance = _blob_radiance((side, side), truth, sigma)
            cen_on = _measured_centroid(_render_y(radiance, spec_on, camera), background_on)
            cen_off = _measured_centroid(_render_y(radiance, spec_off, camera), background_off)
            bias_on = float(np.hypot(cen_on[0] - truth[0], cen_on[1] - truth[1]))
            bias_off = float(np.hypot(cen_off[0] - truth[0], cen_off[1] - truth[1]))
            rows.append(
                RoundTripRow(
                    size_px=size,
                    phase=phase,
                    bias_off_px=bias_off,
                    bias_on_px=bias_on,
                    added_bias_px=bias_on - bias_off,
                )
            )
    return rows


def notes_document(rows: list[RoundTripRow]) -> str:
    by_size: dict[float, list[RoundTripRow]] = {}
    for row in rows:
        by_size.setdefault(row.size_px, []).append(row)

    lines: list[str] = []
    a = lines.append
    a("# D3 sensor-model notes")
    a("")
    a("**Label: every number below is Modeled** unless a row is explicitly")
    a("marked Measured. Nothing is Measured until the bench results land.")
    a("")
    a("## Bayer round-trip experiment (design open question 1)")
    a("")
    a("Noiseless Gaussian blob of known FWHM and sub-pixel phase through the")
    a("§5.1 chain with every stage disabled except radiance scaling, ADC")
    a("quantization, and the ISP tail — isolating mosaic+demosaic ON vs OFF")
    a("(BGGR, Provisional). Bias = |measured centroid - injected truth| of the")
    a("background-subtracted U8 Y output, in full-resolution pixels.")
    a("")
    a("| Blob FWHM (px) | Phase (px) | Bias, mosaic OFF (px) | Bias, mosaic ON (px) | "
      "Added bias (px) |")
    a("| --- | --- | --- | --- | --- |")
    for size in sorted(by_size):
        for row in by_size[size]:
            a(
                f"| {row.size_px:.0f} | {row.phase:.2f} | {row.bias_off_px:.4f} | "
                f"{row.bias_on_px:.4f} | {row.added_bias_px:+.4f} |"
            )
    a("")
    worst = max(rows, key=lambda r: abs(r.added_bias_px))
    small = [r for r in rows if r.size_px <= 3.0]
    worst_small = max(small, key=lambda r: abs(r.added_bias_px))
    mean_small = float(np.mean([abs(r.added_bias_px) for r in small]))
    a("### Reading (Modeled)")
    a("")
    a(
        f"- Worst added bias across the sweep: {worst.added_bias_px:+.4f} px at "
        f"FWHM {worst.size_px:.0f} px, phase {worst.phase:.2f}."
    )
    a(
        f"- For 2-3 px blobs (the D4 tripwire regime): mean added bias "
        f"{mean_small:.4f} px, worst {worst_small.added_bias_px:+.4f} px at phase "
        f"{worst_small.phase:.2f}."
    )
    a("- Feed to D4: compare these numbers against the 0.75 px edge-only")
    a("  centroid tripwire in `configs/exp001_scene.yaml`; the mosaic")
    a("  round-trip consumes part of that budget on the smallest targets.")
    a("")
    a("## Conversion-gain bench anchor (Measured — pending)")
    a("")
    a("Reserved for Samuel's bench results (D3 brief bench task 1): photon")
    a("transfer pairs on the SC3336/Luckfox, slope of (pair-difference")
    a("variance / 2) vs mean. Record here as Measured, with the RAW vs")
    a("post-ISP caveat if vendor tools only expose post-ISP Y. The CFA")
    a("pattern check (bench task 2) also lands here; the mosaic stage stays")
    a("Provisional-BGGR until it does.")
    a("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Bayer round-trip experiment (Modeled)")
    parser.add_argument("--out", type=str, required=True)
    args = parser.parse_args(argv)
    rows = run_round_trip()
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(notes_document(rows))
    print(f"wrote {args.out} ({len(rows)} sweep rows)")


if __name__ == "__main__":
    main()
