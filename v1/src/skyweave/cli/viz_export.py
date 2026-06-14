from __future__ import annotations

import argparse
import sys
from pathlib import Path

from skyweave.config import load_config
from skyweave.viz.exporter import export_synthetic_viz_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a deterministic visualization replay bundle.")
    parser.add_argument("--config", default="configs/sim.yaml", help="Path to the simulation YAML config.")
    parser.add_argument("--output", default="data/viz/synthetic", help="Output directory. Must not already exist.")
    parser.add_argument("--frames", type=int, default=None, help="Optional maximum number of frames to export.")
    args = parser.parse_args(argv)

    output = Path(args.output)
    if output.exists():
        parser.error(f"output directory already exists: {output}")
    config = load_config(args.config)
    root = export_synthetic_viz_bundle(config, output, config_path=args.config, max_frames=args.frames)
    print(f"viz_bundle_path={root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
