# Skyweave Web Visualizer

Static browser assets for the live Skyweave visualizer.

Run from the repo root:

```bash
.venv/bin/python -m pip install -e '.[dev,viz]'
.venv/bin/skyweave-viz-demo
```

Then open `http://localhost:8080`.

The frontend serves vendored Cesium.js and Three.js from `viz_web/vendor`. There is no build step.

Detailed usage and integration notes live in `docs/visualizer.md`.
