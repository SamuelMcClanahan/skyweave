# Skyweave Visualization

Anduril-inspired tactical Three.js visualizer for the Skyweave drone/aircraft tracking system.

## Features

### Visual Design
- **Anduril Aesthetic**: Dark theme with neon cyan/blue accents, glowing effects, bloom post-processing
- **Cesium.js Integration**: 3D Earth with terrain and satellite imagery
- **Glass Morphism UI**: Semi-transparent panels with backdrop blur

### 3D Visualization
- **Camera Nodes**: Geometric representation with frustum wireframes
- **Tracks**: Glowing spheres with velocity arrows and decaying trails
- **Voxel Cloud**: Point cloud with color-coded scores and temporal decay
- **Ray Visualization**: Camera rays to tracked targets (toggle all/per-camera)
- **World Grid**: Adaptive grid with distance markers

### UI Components
- **Left Sidebar**: System status, active track list, camera status
- **Right Sidebar**: View controls, scale slider, ray visualization controls
- **Bottom Bar**: Visibility toggles for all scene elements
- **Floating Panel**: Detailed track metrics (position, velocity, altitude, heading, acceleration)
- **Top Status Bar**: Real-time FPS, latency, track count, connection status

### Interaction
- **Click Tracks**: Select track to view detailed metrics
- **Follow Mode**: Camera follows selected track
- **Auto-Zoom**: Automatically scales view based on track classification
- **Manual Controls**: OrbitControls with scroll wheel zoom

### Data Streaming
- **WebSocket**: Live data streaming from backend
- **Auto-Reconnect**: Automatic reconnection on disconnect
- **Low Latency**: ~30Hz update rate

## Quick Start

### 1. Install Dependencies

The visualizer uses CDN-hosted libraries (no build step required), but you need Python dependencies for the backend:

```bash
cd /Users/samuelmccanahan/Desktop/skyweave
source .venv/bin/activate
pip install aiohttp
```

### 2. Run Demo Server

Test the visualizer with simulated data:

```bash
python -m skyweave.viz.demo
```

Then open http://localhost:8080 in your browser.

You should see:
- 3 cameras in triangular formation
- 1 track moving in a paper airplane arc
- Voxel cloud following the track
- Real-time metrics updating

### 3. Integrate with Skyweave Pipeline

To integrate with your actual tracking pipeline:

```python
from skyweave.viz.server import VizServer, build_viz_frame

# Initialize server
viz_server = VizServer(viz_dir=Path("viz_web"), host="0.0.0.0", port=8080)
await viz_server.start()

# In your tracking loop, broadcast VizFrame
viz_frame = build_viz_frame(
    tracks=[track.model_dump() for track in active_tracks],
    cameras=[camera_to_dict(cam) for cam in cameras.values()],
    weavefield_history=[vol.model_dump() for vol in weavefield_history],
    measurements=[m.model_dump() for m in measurements],
    stats={"fps": fps, "latency_p50_ms": latency},
    ts_ns=current_ts_ns,
)

await viz_server.broadcast_viz_frame(viz_frame)
```

## Architecture

```
viz_web/
├── index.html              # Main HTML shell
├── styles/
│   ├── main.css           # Global styles
│   ├── anduril-theme.css  # Dark theme, neon colors
│   └── panels.css         # Sidebar/panel layouts
├── src/
│   ├── main.js            # Application entry point
│   ├── data/
│   │   ├── state.js       # Global state management
│   │   └── wsclient.js    # WebSocket client
│   ├── renderer/
│   │   ├── scene.js       # Three.js + Cesium setup
│   │   ├── cameras.js     # Camera node rendering
│   │   ├── tracks.js      # Track sphere and trail rendering
│   │   ├── voxels.js      # Voxel point cloud rendering
│   │   ├── rays.js        # Ray visualization
│   │   └── grid.js        # World grid rendering
│   └── ui/
│       └── ui-manager.js  # UI interactions and updates
```

## Controls

### Mouse
- **Left Click + Drag**: Rotate view (OrbitControls)
- **Scroll Wheel**: Zoom in/out
- **Right Click + Drag**: Pan view
- **Click Track**: Select track for detailed view

### UI Toggles (Bottom Bar)
- **Cameras**: Show/hide camera nodes
- **Frustums**: Show/hide camera frustums
- **Voxels**: Show/hide voxel point cloud
- **Tracks**: Show/hide track spheres
- **Trails**: Show/hide track trails
- **Grid**: Show/hide world grid
- **Earth**: Show/hide Cesium Earth

### Ray Visualization (Right Sidebar)
- **None**: No rays
- **All Cameras**: Rays from all cameras to all tracks
- **Camera X Only**: Rays from specific camera only

### View Controls (Right Sidebar)
- **Scale Slider**: Manual scale adjustment (1m - 10km)
- **Auto-Zoom**: Automatically zoom based on track classification

## Data Format

### VizFrame (WebSocket JSON)

```json
{
  "ts_ns": 1234567890123456789,
  "tracks": [
    {
      "id": 1,
      "state": [x, y, z, vx, vy, vz],
      "covariance": [[...], ...],
      "status": "active",
      "classification": "drone",
      "classification_confidence": 0.85,
      "created_ts_ns": 1234567890000000000,
      "last_update_ts_ns": 1234567890123456789,
      "update_count": 42,
      "miss_count": 0,
      "trail": [[x, y, z, ts], ...]
    }
  ],
  "cameras": [
    {
      "id": 0,
      "position": [x, y, z],
      "rotation_quat": [x, y, z, w],
      "fov_h_deg": 60.0,
      "fov_v_deg": 45.0,
      "fps": 30.0,
      "online": true
    }
  ],
  "weavefield_history": [
    {
      "ts_ns": 1234567890123456789,
      "grid": {
        "frame_id": "world",
        "origin": [x, y, z],
        "voxel_size_m": 0.10,
        "dims": [nx, ny, nz]
      },
      "voxels": [
        {"ix": 10, "iy": 20, "iz": 5, "score": 3.5},
        ...
      ],
      "peaks": [],
      "decay_s": 1.0,
      "source_packet_ids": ["cam0", "cam1"]
    }
  ],
  "measurements": [],
  "stats": {
    "fps": 30.0,
    "latency_p50_ms": 25.0,
    "n_tracks": 1,
    "n_voxels": 500
  }
}
```

## Classification System

The visualizer supports classification-based styling:

- **drone**: Cyan color, auto-zoom to 50m
- **plane**: Blue color, auto-zoom to 2000m  
- **bird**: Yellow color, auto-zoom to 20m
- **unknown**: White color, auto-zoom to 100m

Set `track.classification` and `track.classification_confidence` in your tracking data.

## Deployment

### Local Development
```bash
python -m skyweave.viz.demo
# Open http://localhost:8080
```

### Remote Access (Ngrok)
```bash
ngrok http 8080
# Share the ngrok URL
```

### Production (Docker + HTTPS)
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install aiohttp
CMD ["python", "-m", "skyweave.viz.server"]
```

Deploy with reverse proxy (nginx/Caddy) for HTTPS.

## Troubleshooting

### WebSocket Not Connecting
- Check that backend is running on port 8080
- Check browser console for errors
- Verify firewall allows WebSocket connections

### Poor Performance
- Reduce `top_k_voxels` in backend (default 5000)
- Disable bloom effect (comment out in scene.js)
- Lower WebSocket update rate

### Cesium Not Loading
- Check Cesium Ion token (free tier available)
- Can disable Earth view with bottom bar toggle
- Check browser console for Cesium errors

## Future Enhancements (Phase 5-6)

- [ ] Replay system with timeline scrubber
- [ ] Session management and recording browser
- [ ] Downsampling for remote viewers
- [ ] Export track data (CSV/JSON)
- [ ] Screenshot/video recording
- [ ] Multiple simultaneous tracks
- [ ] Historical comparison view
- [ ] Custom map overlays

## License

Part of the Skyweave project.
