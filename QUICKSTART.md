# 🚀 Skyweave Visualizer - Quick Start

## ✅ STATUS: Running and Ready!

**Server Running:** http://localhost:8080 (PID: 11704)

---

## 📖 Quick Reference

### Start the Visualizer
```bash
cd /Users/samuelmccanahan/Desktop/skyweave
.venv/bin/python -m skyweave.viz.demo
```

### Stop the Server
```bash
# Find and kill the process
lsof -ti:8080 | xargs kill
```

### Access the Visualizer
**Local:** http://localhost:8080  
**Remote:** Use ngrok or deploy to cloud

---

## 🎮 Controls

| Action | Control |
|--------|---------|
| **Rotate View** | Left Click + Drag |
| **Zoom** | Scroll Wheel |
| **Pan View** | Right Click + Drag |
| **Select Track** | Click on track sphere |
| **Follow Track** | Click track → "Follow Camera" button |

---

## 🎨 UI Elements

### Top Bar
- **Track Count:** Active tracks
- **FPS:** Rendering performance
- **Latency:** Data stream latency
- **Connection:** WebSocket status (green = connected)

### Left Sidebar
- **System Status:** Cameras, tracks, processing rate, voxels
- **Active Tracks:** Click to select and view details
- **Cameras:** Online/offline status

### Right Sidebar
- **Scale Slider:** Manual zoom control (1m - 10km)
- **Auto-Zoom:** Automatic zoom based on track type
- **Ray Visualization:**
  - None: No rays
  - All Cameras: Rays from all cameras
  - Camera X Only: Specific camera rays

### Bottom Bar (Toggles)
- 🎥 **Cameras:** Show/hide camera nodes
- 📐 **Frustums:** Show/hide camera FOV
- 📍 **Voxels:** Show/hide point cloud
- 🎯 **Tracks:** Show/hide track spheres
- ━ **Trails:** Show/hide track history
- ⊞ **Grid:** Show/hide world grid
- 🌍 **Earth:** Show/hide Cesium terrain

### Floating Panel (Click track to open)
- Classification & confidence
- Position (X, Y, Z)
- Altitude, speed, heading
- Climb rate, acceleration
- Track age, update count
- **Follow Camera** button
- **Center View** button

---

## 🎨 Color Coding

### Track Status
- **Cyan:** Active track
- **Orange:** Candidate track
- **Red:** Coasting track (no recent updates)

### Track Classification
- **Cyan:** Drone (auto-zoom to 50m)
- **Blue:** Plane (auto-zoom to 2000m)
- **Yellow:** Bird (auto-zoom to 20m)
- **White:** Unknown (auto-zoom to 100m)

### Voxels
- **Dark Blue:** Low score
- **Cyan:** Medium score
- **White:** High score

### Camera Rays
- **Cyan:** Camera 0
- **Blue:** Camera 1
- **Magenta:** Camera 2
- **Orange:** Camera 3+

---

## 📊 What You're Seeing (Demo Data)

The demo shows:
- **3 cameras** in triangular formation at ~2m height
- **1 track** moving in a paper airplane arc (3 seconds each direction)
- **Voxel cloud** of ~100-200 voxels following the track
- **30 Hz update rate** (smooth real-time visualization)

The track loops continuously for testing.

---

## 🔌 Integrate with Your Pipeline

Replace demo data with your actual tracking data:

```python
from skyweave.viz.server import VizServer, build_viz_frame

# Initialize server
viz_server = VizServer(viz_dir=Path("viz_web"))
await viz_server.start()

# Broadcast data (in your tracking loop)
viz_frame = build_viz_frame(
    tracks=[track.model_dump() for track in tracks],
    cameras=[cam_to_dict(c) for c in cameras.values()],
    weavefield_history=[v.model_dump() for v in volumes],
    measurements=[m.model_dump() for m in measurements],
    stats={"fps": fps, "latency_p50_ms": latency},
    ts_ns=current_ts_ns,
)
await viz_server.broadcast_viz_frame(viz_frame)
```

See `VISUALIZER_SUMMARY.md` for detailed integration guide.

---

## 🌐 Share with Others

### Option 1: Ngrok (Easiest)
```bash
ngrok http 8080
# Share the https://xxx.ngrok.io URL
```

### Option 2: Cloudflare Tunnel
```bash
cloudflared tunnel --url http://localhost:8080
```

### Option 3: Deploy to Cloud
Deploy to AWS/GCP/DigitalOcean with HTTPS via nginx/Caddy

---

## 🐛 Troubleshooting

### WebSocket Won't Connect
- Check server is running: `lsof -ti:8080`
- Check browser console for errors (F12)
- Verify firewall allows WebSocket connections

### Poor Performance
- Toggle off Earth view (bottom bar)
- Reduce ray visualization (use per-camera instead of all)
- Check FPS counter in top bar
- Close other browser tabs

### Cesium Not Loading
- Check browser console for Cesium errors
- Verify internet connection (Cesium loads from CDN)
- Can work without Cesium - just toggle Earth off

### Server Won't Start
- Check port 8080 is not in use: `lsof -ti:8080`
- Kill existing process: `kill $(lsof -ti:8080)`
- Check Python dependencies: `uv pip list | grep aiohttp`

---

## 📚 Documentation

- **Full Documentation:** `viz_web/README.md`
- **Implementation Summary:** `VISUALIZER_SUMMARY.md`
- **Architecture Plan:** `.claude/plan.md`
- **Project Spec:** `SPEC_MVP.md`

---

## ✨ Features Implemented

✅ Anduril-inspired dark theme with neon glows  
✅ Three.js + Cesium.js 3D visualization  
✅ Click-to-select tracks with detail panel  
✅ Camera ray visualization (all/per-camera)  
✅ Auto-zoom based on classification  
✅ Real-time WebSocket streaming  
✅ Temporal voxel decay  
✅ Track trails with gradient fade  
✅ Velocity arrows on tracks  
✅ System status and analytics  
✅ Responsive UI with glass morphism  

---

**🎉 Your visualizer is running! Open http://localhost:8080 now!**
