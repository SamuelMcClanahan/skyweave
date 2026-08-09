"""S1 (mosaic/ISP) and S4 (Bayer round-trip experiment)."""

from __future__ import annotations

import numpy as np

from skyweave2.sensor_model import SensorModelSpec
from skyweave2.sensor_model.experiments import main as experiments_main
from skyweave2.sensor_model.experiments import notes_document, run_round_trip
from skyweave2.sensor_model.mosaic import (
    cfa_channel_map,
    demosaic,
    dn_to_y_u8,
    isp_to_y_u8,
    mosaic,
)


def test_bggr_channel_map_hand_case():
    cfa = cfa_channel_map("BGGR", (4, 4))
    # BGGR tile: (0,0)=B(2) (0,1)=G(1) / (1,0)=G(1) (1,1)=R(0).
    assert cfa[0, 0] == 2 and cfa[0, 1] == 1
    assert cfa[1, 0] == 1 and cfa[1, 1] == 0
    assert cfa[2, 2] == 2  # tiles repeat with period 2


def test_mosaic_samples_the_right_channel():
    spec = SensorModelSpec(cfa_pattern="BGGR")
    rgb = np.zeros((2, 2, 3))
    rgb[:, :, 0] = 100.0  # R
    rgb[:, :, 1] = 200.0  # G
    rgb[:, :, 2] = 300.0  # B
    raw = mosaic(rgb, spec)
    assert raw[0, 0] == 300.0 and raw[1, 1] == 100.0
    assert raw[0, 1] == 200.0 and raw[1, 0] == 200.0


def test_demosaic_flat_field_is_exact():
    spec = SensorModelSpec(cfa_pattern="BGGR")
    rgb = np.full((8, 8, 3), 500.0)
    out = demosaic(mosaic(rgb, spec), spec)
    assert np.allclose(out, 500.0, atol=1e-9)  # normalized interpolation: exact


def test_demosaic_recovers_distinct_color_channels():
    """Colored input: a CFA-phase or channel-mixing bug (e.g. demosaic reading
    RGGB against a BGGR mosaic) swaps R and B and cannot pass this."""
    spec = SensorModelSpec(cfa_pattern="BGGR")
    rgb = np.zeros((10, 10, 3))
    rgb[:, :, 0] = 100.0
    rgb[:, :, 1] = 200.0
    rgb[:, :, 2] = 300.0
    out = demosaic(mosaic(rgb, spec), spec)
    interior = out[2:-2, 2:-2]
    assert np.allclose(interior[:, :, 0], 100.0, atol=1e-9)
    assert np.allclose(interior[:, :, 1], 200.0, atol=1e-9)
    assert np.allclose(interior[:, :, 2], 300.0, atol=1e-9)


def test_demosaic_bilinear_is_exact_on_linear_color_gradients():
    """Bilinear interpolation reproduces any per-channel linear ramp exactly
    in the interior; wrong CFA phase samples the wrong ramp and fails."""
    spec = SensorModelSpec(cfa_pattern="BGGR")
    v, u = np.meshgrid(np.arange(12, dtype=np.float64), np.arange(16, dtype=np.float64),
                       indexing="ij")
    rgb = np.stack([100.0 + 3.0 * u, 200.0 + 2.0 * v, 50.0 + u + 5.0 * v], axis=2)
    out = demosaic(mosaic(rgb, spec), spec)
    interior = slice(2, -2)
    assert np.allclose(out[interior, interior], rgb[interior, interior], atol=1e-9)


def test_isp_hand_case_gray():
    """Gray input: Y = ((dn - black) / (max - black)) ** (1/gamma) * 255."""
    spec = SensorModelSpec(black_level_dn=64.0, adc_bits=10, gamma=2.2)
    dn = np.full((1, 1, 3), 543.5)
    expected = round(((543.5 - 64.0) / 959.0) ** (1.0 / 2.2) * 255.0)
    assert isp_to_y_u8(dn, spec)[0, 0] == expected


def test_isp_y_weights():
    spec = SensorModelSpec(black_level_dn=0.0, adc_bits=10, gamma=1.0,
                           y_weights=(0.299, 0.587, 0.114))
    dn = np.zeros((1, 1, 3))
    dn[0, 0] = (1023.0, 0.0, 0.0)
    assert isp_to_y_u8(dn, spec)[0, 0] == round(0.299 * 255)


def test_unknown_cfa_pattern_rejected():
    try:
        cfa_channel_map("XYZW", (4, 4))
    except ValueError as err:
        assert "XYZW" in str(err)
    else:  # pragma: no cover
        raise AssertionError("unknown CFA must raise")


def test_s4_mosaic_toggle_changes_small_blob_centroid():
    """S4: the round-trip adds measurable centroid bias for small blobs."""
    rows = run_round_trip(sizes_px=(2.0, 3.0), phases=(0.25, 0.5))
    assert len(rows) == 4
    # The mosaic path must actually differ from the clean path somewhere.
    assert any(abs(r.added_bias_px) > 1e-3 for r in rows)
    # And small-blob bias stays well under a pixel (sanity on the harness).
    assert all(r.bias_on_px < 1.0 for r in rows)


def test_s4_experiment_writes_notes_table(tmp_path):
    out = tmp_path / "notes.md"
    experiments_main(["--out", str(out)])
    text = out.read_text()
    assert "Bayer round-trip experiment" in text
    assert text.count("\n| ") >= 32  # 8 sizes x 4 phases of table rows
    assert "Modeled" in text
    assert "Measured — pending" in text


def test_notes_document_is_deterministic():
    rows = run_round_trip(sizes_px=(2.0,), phases=(0.0, 0.5))
    assert notes_document(rows) == notes_document(rows)


def test_dn_to_y_u8_paths_agree_on_flat_field():
    spec_on = SensorModelSpec(enable_mosaic=True)
    spec_off = SensorModelSpec(enable_mosaic=False)
    rgb = np.full((8, 8, 3), 400.0)
    assert np.array_equal(dn_to_y_u8(rgb, spec_on), dn_to_y_u8(rgb, spec_off))
