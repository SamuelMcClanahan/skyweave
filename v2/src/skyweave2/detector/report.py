"""D4 detector report generator (seeded, deterministic).

One command produces ``D4_DETECTOR_REPORT.md``: scorecards for mog2 and
ive_approx at the three swept resolutions on the gate clip and the negative
clip, the repeatability sigma/bias with the tripwire verdict, warm-up
measurement, and the false-per-frame table. Every number labeled.

Determinism contract: the report body is byte-identical for identical
inputs and seed. Perf metrics are inherently wall-clock measurements; the
report zeroes them in the deterministic body and (optionally) writes live
scorecard JSONs beside the report as non-deterministic evidence.

Usage (from ``v2/``):

    uv run python -m skyweave2.detector.report --seed 7 \
        --gate <gate-clip-dir> --negative <negative-clip-dir> \
        --labels <labels.jsonl> --out docs/D4_DETECTOR_REPORT.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skyweave2.detector.config import Backend, DetectorConfig
from skyweave2.detector.repeatability import (
    build_noise_fixture_clip,
    measure_repeatability,
)
from skyweave2.detector.runner import detect_clip, scorecard_for_detections
from skyweave2.eval.labels import TruthLabel, read_labels
from skyweave2.sensor_model import SensorModelSpec

RESOLUTIONS = ((2304, 1296), (1536, 864), (1152, 648))
PRIMARY = (1536, 864)
BACKENDS = (Backend.MOG2, Backend.IVE_APPROX)
TRIPWIRE_PX = 0.75  # full-res edge-only centroid sigma (D2 rule)


def _fmt(x, digits: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def _detector_config(
    backend: Backend, width: int, height: int, base: DetectorConfig
) -> DetectorConfig:
    return base.model_copy(
        update={"backend": backend, "proc_width": width, "proc_height": height}
    )


def generate_report(
    gate_clip: str,
    negative_clip: str,
    labels: list[TruthLabel],
    seed: int,
    base_config: DetectorConfig,
    scratch_dir: Path,
    evidence_dir: Path | None = None,
) -> str:
    cards: dict[tuple[str, str, tuple[int, int]], dict] = {}
    for backend in BACKENDS:
        for width, height in RESOLUTIONS:
            config = _detector_config(backend, width, height, base_config)
            for clip_name, clip_dir, clip_labels in (
                ("gate", gate_clip, labels),
                ("negative", negative_clip, []),
            ):
                _, results = detect_clip(clip_dir, config)
                card = scorecard_for_detections(
                    clip_dir, config, results, labels=clip_labels, deterministic_perf=True
                )
                cards[(backend.value, clip_name, (width, height))] = card
                if evidence_dir is not None:
                    live = scorecard_for_detections(
                        clip_dir, config, results, labels=clip_labels,
                        deterministic_perf=False,
                    )
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    name = f"{backend.value}_{clip_name}_{width}x{height}.json"
                    (evidence_dir / name).write_text(
                        json.dumps(live, indent=2, sort_keys=True) + "\n"
                    )

    # Repeatability at the primary resolution, on the mid-bracket sensor
    # model fixture (Modeled until the conversion-gain bench lands).
    primary_config = _detector_config(Backend.MOG2, *PRIMARY, base_config)
    fixture_dir = scratch_dir / "repeatability_fixture"
    full_w, full_h = 768, 432  # fixture full-res grid; sigma scales via the D0 law
    # AE is OFF in this fixture: its global luma drift inflates the
    # background model's variance and measures AE response, not centroid
    # repeatability. Shot/read/FPN noise stay at the mid-bracket defaults.
    # Blob contrast is target-like (the gate target is a near-silhouette).
    fixture_spec = SensorModelSpec(enable_ae=False)
    truths = build_noise_fixture_clip(
        fixture_dir,
        width=full_w,
        height=full_h,
        truth_uv=(full_w * 0.25, full_h * 0.35),
        fwhm_px=4.0,
        n_frames=primary_config.warmup_frames + 120,
        dataset_seed=seed,
        spec=fixture_spec,
        blob_radiance=2.0,
        appear_after=primary_config.warmup_frames,
    )
    # Fixture proc/full ratio matches the primary sweep's ratio to ITS full
    # resolution (RESOLUTIONS[0]), so sigma is full-res-equivalent in the
    # tripwire's sense regardless of fixture canvas size.
    ratio_u = PRIMARY[0] / RESOLUTIONS[0][0]
    ratio_v = PRIMARY[1] / RESOLUTIONS[0][1]
    fixture_config = primary_config.model_copy(
        update={
            "proc_width": max(1, round(full_w * ratio_u)),
            "proc_height": max(1, round(full_h * ratio_v)),
        }
    )
    rep = measure_repeatability(fixture_dir, fixture_config, truths)
    # The tripwire fires if EITHER axis exceeds the budget: a per-axis
    # overrun is not excused by the other axis being clean.
    worst_axis_sigma = max(rep.sigma_u_px, rep.sigma_v_px)
    verdict_fires = worst_axis_sigma > TRIPWIRE_PX

    warmup_cards = [
        cards[(b.value, "gate", PRIMARY)] for b in BACKENDS
    ]

    lines: list[str] = []
    a = lines.append
    a("# D4 detector report")
    a("")
    a("**Labels.** Scorecard quality/health numbers on the L4-rendered clips are")
    a("Measured (recorded run of a frozen configuration on the golden inputs).")
    a("The repeatability sigma and the tripwire verdict are **Modeled**: the")
    a("sensor-noise bracket is mid-bracket until the conversion-gain bench")
    a("lands, after which the verdict is re-checked. Perf numbers are zeroed in")
    a("this deterministic body; live-perf scorecard JSONs sit beside the report")
    a("as non-deterministic evidence.")
    a("")
    a(f"Seed {seed}; detector rev {base_config.detector_rev}; generated by")
    a("`uv run python -m skyweave2.detector.report` (byte-identical for")
    a("identical inputs and seed).")
    a("")
    a("## Scorecards: gate clip (quality + health)")
    a("")
    a("| Backend | Resolution | Recall | Centroid err mean (px) | p95 (px) | "
      "False/frame | Occupancy % | Warm-up frames | Pass |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for backend in BACKENDS:
        for res in RESOLUTIONS:
            card = cards[(backend.value, "gate", res)]
            q, h = card["quality"], card["health"]
            a(
                f"| {backend.value} | {res[0]}x{res[1]} | {_fmt(q['recall'])} | "
                f"{_fmt(q['centroid_err_px_mean'])} | {_fmt(q['centroid_err_px_p95'])} | "
                f"{_fmt(q['false_per_frame'])} | {_fmt(h['occupancy_pct'])} | "
                f"{h['warmup_frames']} | {card['pass']['overall']} |"
            )
    a("")
    a("## False proposals: negative clip (no target rendered)")
    a("")
    a("| Backend | Resolution | False/frame | Occupancy % | Component count |")
    a("| --- | --- | --- | --- | --- |")
    for backend in BACKENDS:
        for res in RESOLUTIONS:
            card = cards[(backend.value, "negative", res)]
            q, h = card["quality"], card["health"]
            a(
                f"| {backend.value} | {res[0]}x{res[1]} | {_fmt(q['false_per_frame'])} | "
                f"{_fmt(h['occupancy_pct'])} | {_fmt(h['component_count'])} |"
            )
    a("")
    a("## Centroid repeatability at the primary resolution (Modeled)")
    a("")
    a(f"- Fixture: drifting 4 px FWHM blob, {rep.n_frames} scored frames, fresh")
    a("  mid-bracket sensor noise per frame (AE off: stationary background so")
    a("  the scatter is noise-driven, not AE response), proc/full ratio matched")
    a(f"  to {PRIMARY[0]}x{PRIMARY[1]} / {RESOLUTIONS[0][0]}x{RESOLUTIONS[0][1]}; "
      "mog2 backend. Error is per-frame against that frame's truth, so the")
    a("  drift itself adds nothing; the u axis carries motion-edge artifacts,")
    a("  the v axis is the clean noise floor.")
    a(f"- Sigma (full-res equivalent): u {_fmt(rep.sigma_u_px)} px, "
      f"v {_fmt(rep.sigma_v_px)} px, combined {_fmt(rep.sigma_px)} px.")
    a(f"- Bias: u {_fmt(rep.bias_u_px)} px, v {_fmt(rep.bias_v_px)} px "
      f"(matched {rep.n_matched}/{rep.n_frames} frames).")
    a("")
    a(f"### Tripwire verdict (D2 rule, threshold {TRIPWIRE_PX} px, worst axis)")
    a("")
    if verdict_fires:
        a(f"**FIRES (Modeled): worst-axis sigma {_fmt(worst_axis_sigma)} px > "
          f"{TRIPWIRE_PX} px.** The")
        a("authoritative centroid switches to patch-refined per the D2 decision,")
        a("pending re-check against the Measured conversion gain.")
    else:
        a(f"**CLEAR (Modeled): worst-axis sigma {_fmt(worst_axis_sigma)} px <= "
          f"{TRIPWIRE_PX} px.** The")
        a("edge-only centroid path stands, pending re-check against the Measured")
        a("conversion gain.")
    a("")
    a("## Warm-up")
    a("")
    a(f"- Schedule: {base_config.warmup_frames} frames = "
      f"{base_config.warmup_frames / base_config.fps:.1f} s (manifest warm-up 3.0 s).")
    for backend, card in zip(BACKENDS, warmup_cards, strict=True):
        a(
            f"- {backend.value}: measured warmup_frames "
            f"{card['health']['warmup_frames']} (schedule + occupancy settling)."
        )
    a("- Reading note: no observation is emitted before the 90-frame schedule")
    a("  (test K5). The larger settling number is the health metric tracking")
    a("  the TARGET's apparent-size drift across the crossing — on clean")
    a("  synthetic clips the target is the only foreground, so occupancy")
    a("  \"steady state\" follows the target, not the background model. On")
    a("  real sky footage (background-dominated) the metric reads normally.")
    a("")
    a("## Comparable-scorecard note (§7.3)")
    a("")
    a("mog2 and ive_approx are compared by the tables above, never by mask")
    a("bit-parity. Divergences in centroid error or false-per-frame between the")
    a("two are findings for D8 board prediction; mask differences are not.")
    a("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate D4_DETECTOR_REPORT.md")
    parser.add_argument("--gate", required=True)
    parser.add_argument("--negative", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", help="base DetectorConfig JSON (optional)")
    parser.add_argument("--scratch", default="/tmp/d4_report_scratch")
    parser.add_argument(
        "--evidence-dir",
        help="also write live-perf scorecard JSONs here (non-deterministic)",
    )
    args = parser.parse_args(argv)
    base = (
        DetectorConfig.model_validate(json.loads(Path(args.config).read_text()))
        if args.config
        else DetectorConfig()
    )
    report = generate_report(
        args.gate,
        args.negative,
        read_labels(args.labels),
        args.seed,
        base,
        Path(args.scratch),
        Path(args.evidence_dir) if args.evidence_dir else None,
    )
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
