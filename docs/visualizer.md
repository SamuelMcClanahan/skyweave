# Skyweave Visualizer

The visualizer is a browser UI for Skyweave `VizFrame` streams. It keeps the
original intended setup:

- Cesium.js for the Earth/terrain context
- Three.js overlay for cameras, rays, voxels, tracks, trails, and labels
- `aiohttp` WebSocket server for live frames
- no frontend build step

## Install

```bash
.venv/bin/python -m pip install -e '.[dev,viz]'
```

Use `.[dev,viz,numba]` when running the optimized backend and visualizer on the
same machine.

## Demo

```bash
.venv/bin/skyweave-viz-demo
```

Open `http://localhost:8080`.

The demo streams synthetic camera, track, and voxel data over `/ws`.

## Data Shape

The browser expects one JSON `VizFrame` per WebSocket message:

```json
{
  "ts_ns": 123,
  "tracks": [],
  "cameras": [],
  "measurements": [],
  "weavefield_history": [],
  "stats": {}
}
```

Use the Pydantic models in `src/skyweave/messages.py` as the source of truth for
`Track`, `VizCamera`, `Measurement3D`, `WeavefieldVolume`, and `VizFrame`.

## Integration

```python
from pathlib import Path

from skyweave.viz.server import VizServer, build_viz_frame

server = VizServer(viz_dir=Path("viz_web"), host="0.0.0.0", port=8080)
await server.start()

frame = build_viz_frame(
    tracks=[track.model_dump(mode="json") for track in tracks],
    cameras=[camera_payload],
    weavefield_history=[volume.model_dump(mode="json")],
    measurements=[measurement.model_dump(mode="json")],
    stats={"fps": 100.0, "latency_p50_ms": 8.0},
    ts_ns=ts_ns,
)
await server.broadcast_viz_frame(frame)
```

The visualizer should consume the message contract only. It should not import
Rayweave scorer internals.

## Files

```text
viz_web/
  index.html
  styles/
  src/data/
  src/renderer/
  src/ui/

src/skyweave/viz/
  server.py
  demo.py
```

## Notes

- Cesium and Three.js load from CDNs, so the browser needs internet access.
- The current demo uses a local San Francisco ENU origin for terrain context.
- If Cesium fails to load, check browser console output and network access.
