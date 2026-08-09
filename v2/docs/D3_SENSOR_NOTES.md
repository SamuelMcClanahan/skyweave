# D3 sensor-model notes

**Label: every number below is Modeled** unless a row is explicitly
marked Measured. Nothing is Measured until the bench results land.

## Bayer round-trip experiment (design open question 1)

Noiseless Gaussian blob of known FWHM and sub-pixel phase through the
§5.1 chain with every stage disabled except radiance scaling, ADC
quantization, and the ISP tail — isolating mosaic+demosaic ON vs OFF
(BGGR, Provisional). Bias = |measured centroid - injected truth| of the
background-subtracted U8 Y output, in full-resolution pixels.

| Blob FWHM (px) | Phase (px) | Bias, mosaic OFF (px) | Bias, mosaic ON (px) | Added bias (px) |
| --- | --- | --- | --- | --- |
| 2 | 0.00 | 0.0000 | 0.0201 | +0.0201 |
| 2 | 0.25 | 0.0052 | 0.0162 | +0.0110 |
| 2 | 0.50 | 0.0000 | 0.0000 | +0.0000 |
| 2 | 0.75 | 0.0052 | 0.0162 | +0.0110 |
| 3 | 0.00 | 0.0000 | 0.0030 | +0.0030 |
| 3 | 0.25 | 0.0029 | 0.0190 | +0.0161 |
| 3 | 0.50 | 0.0000 | 0.0000 | +0.0000 |
| 3 | 0.75 | 0.0029 | 0.0190 | +0.0161 |
| 4 | 0.00 | 0.0000 | 0.0055 | +0.0055 |
| 4 | 0.25 | 0.0025 | 0.0046 | +0.0021 |
| 4 | 0.50 | 0.0000 | 0.0000 | +0.0000 |
| 4 | 0.75 | 0.0025 | 0.0046 | +0.0021 |
| 5 | 0.00 | 0.0000 | 0.0037 | +0.0037 |
| 5 | 0.25 | 0.0012 | 0.0078 | +0.0066 |
| 5 | 0.50 | 0.0000 | 0.0000 | +0.0000 |
| 5 | 0.75 | 0.0012 | 0.0078 | +0.0066 |
| 6 | 0.00 | 0.0000 | 0.0058 | +0.0058 |
| 6 | 0.25 | 0.0001 | 0.0024 | +0.0023 |
| 6 | 0.50 | 0.0000 | 0.0000 | +0.0000 |
| 6 | 0.75 | 0.0001 | 0.0024 | +0.0023 |
| 7 | 0.00 | 0.0000 | 0.0002 | +0.0002 |
| 7 | 0.25 | 0.0112 | 0.0034 | -0.0078 |
| 7 | 0.50 | 0.0000 | 0.0000 | +0.0000 |
| 7 | 0.75 | 0.0112 | 0.0034 | -0.0078 |
| 8 | 0.00 | 0.0000 | 0.0003 | +0.0003 |
| 8 | 0.25 | 0.0039 | 0.0014 | -0.0025 |
| 8 | 0.50 | 0.0000 | 0.0000 | +0.0000 |
| 8 | 0.75 | 0.0039 | 0.0014 | -0.0025 |
| 9 | 0.00 | 0.0000 | 0.0007 | +0.0007 |
| 9 | 0.25 | 0.0049 | 0.0047 | -0.0003 |
| 9 | 0.50 | 0.0000 | 0.0000 | +0.0000 |
| 9 | 0.75 | 0.0049 | 0.0047 | -0.0003 |

### Reading (Modeled)

- Worst added bias across the sweep: +0.0201 px at FWHM 2 px, phase 0.00.
- For 2-3 px blobs (the D4 tripwire regime): mean added bias 0.0097 px, worst +0.0201 px at phase 0.00.
- Feed to D4: compare these numbers against the 0.75 px edge-only
  centroid tripwire in `configs/exp001_scene.yaml`; the mosaic
  round-trip consumes part of that budget on the smallest targets.

## Conversion-gain bench anchor (Measured — pending)

Reserved for Samuel's bench results (D3 brief bench task 1): photon
transfer pairs on the SC3336/Luckfox, slope of (pair-difference
variance / 2) vs mean. Record here as Measured, with the RAW vs
post-ISP caveat if vendor tools only expose post-ISP Y. The CFA
pattern check (bench task 2) also lands here; the mosaic stage stays
Provisional-BGGR until it does.

