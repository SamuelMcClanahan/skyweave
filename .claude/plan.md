# Plan: Anduril-Inspired Three.js Visualizer for Skyweave

## Context Understanding

After reviewing the codebase, I understand:

### Current System Architecture
- **Detection Pipeline**: Multi-camera system using frame differencing or KNN background subtraction
- **Rayweave Engine**: Projects 2D motion evidence into 3D voxel space via ray tracing (DDA algorithm)
- **Weavefield**: Sparse 4D spatiotemporal evidence map (voxels over time)
- **Tracking**: Kalman filter (6D state: position + velocity) tracking voxel peaks
- **Data Flow**: MotionPacket → Rayweave scoring → WeavefieldVolume → VoxelPeak → Measurement3D → Track
- **Recording**: MsgPack-based session recording with motion packets, weavefields, measurements, tracks
- **Scale**: Designed to handle both close-range drones and distant aircraft (20k+ feet)

### Key Message Schemas (messages.py)
- `MotionPacket`: Camera-side motion evidence (blobs, patches, centroids)
- `WeavefieldVolume`: Sparse voxel grid with scores and peaks
- `Track`: Kalman-filtered track with state, covariance, trail history
- `VizFrame`: Browser-ready visualization frame (tracks, cameras, measurements, weavefield history)
- `Measurement3D`: 3D measurements from voxel peaks or triangulation

### Geometry System (fusion/geom.py)
- World frame: right-handed, Z-up, meters
- Camera frame: OpenCV convention (X-right, Y-down, Z-forward)
- Transform: `T_world_cam` maps camera → world
- Ray construction: pixel → undistort → camera ray → world ray

## Design: Anduril-Inspired Visualizer

### Visual Aesthetic (Anduril Style)
- **Dark theme**: Near-black background (#0a0e1a or similar)
- **Neon accents**: Cyan (#00d4ff), electric blue (#0080ff), white highlights
- **Glowing effects**: Emissive materials, bloom post-processing
- **Tactical HUD**: Hexagonal UI elements, technical grid overlays
- **Glass morphism**: Semi-transparent panels with backdrop blur
- **Typography**: Monospace fonts (JetBrains Mono, Fira Code), technical readouts
- **Grid system**: Subtle world grid with distance markers
- **Holographic effects**: Wireframe frustums, glowing voxel points

### Core Visualization Components

#### 1. 3D Scene (Three.js)
- **World Grid**: Adaptive grid that scales from meters to kilometers
- **Camera Nodes**: 
  - Geometric representation (pyramid/frustum mesh)
  - Frustum wireframe showing FOV
  - ID labels with distance readouts
  - Online/offline status indicators
- **Tracks**:
  - Primary sphere/diamond at current position
  - Glowing trail tube/ribbon showing history
  - Velocity vector arrow
  - Altitude readout, speed, heading
  - Classification tag (drone/plane/bird)
  - Covariance ellipsoid (semi-transparent)
- **Weavefield Voxels**:
  - Point cloud or instanced box meshes
  - Color-coded by score (gradient: blue → cyan → white)
  - Temporal decay (alpha fade for older evidence)
  - Support count visualization (brightness)
- **Camera Rays**:
  - Optional: Show rays from cameras to detection points
  - Ray-voxel intersection visualization
  - Triangulation geometry display
- **Measurements**:
  - Voxel peaks: cyan spheres
  - Triangulation estimates: orange spheres
  - Turret observations: red spheres (future)

#### 2. Scale Handling (Meters to 20k feet)
- **Adaptive LOD**: 
  - Near mode (<100m): Full detail, all voxels, camera frustums
  - Mid mode (100m-1km): Simplified voxels, larger grid spacing
  - Far mode (>1km): Track-centric view, reduced voxel detail
- **Camera Controls**:
  - OrbitControls for local inspection
  - Fly controls for long-range navigation
  - Auto-zoom to track bounds
  - Preset views (top-down, follow track, camera POV)
- **Unit Switching**: Meters, feet, nautical miles
- **Altitude Reference**: Show ground plane, sea level markers

#### 3. Google Earth Integration
- **Cesium.js Option**: 
  - Embed CesiumJS for photorealistic Earth context
  - Project tracks onto terrain
  - Sync Three.js overlay with Cesium camera
- **Static Imagery Option**:
  - Fetch Google Maps/Mapbox tiles as ground texture
  - Project onto flat plane or simple terrain mesh
- **Toggle Mode**: Switch between abstract grid and Earth view
- **Coordinate System**: Support lat/lon/alt if calibration includes GPS

#### 4. Left Sidebar: Analytics & Data
**Sections:**
- **System Status**
  - Active tracks count
  - Camera online status (green/red indicators)
  - Processing FPS, latency (p50/p95)
  - Packet loss, dropped frames
- **Track Details** (selected track)
  - Track ID, status (candidate/active/coasting)
  - Position (X/Y/Z or lat/lon/alt)
  - Velocity vector, speed, heading
  - Altitude, altitude rate
  - Classification, confidence
  - Update count, age
  - Covariance eigenvalues
- **Motion Profile**
  - Speed graph (mini sparkline)
  - Acceleration magnitude
  - Turn rate
  - Behavioral classification hints (level flight, hovering, maneuvering)
- **Camera Contributions**
  - Which cameras see the track
  - Per-camera confidence scores
  - Geometric dilution of precision (GDOP) estimate

#### 5. Right Sidebar: Replay Controls
**Sections:**
- **Playback Controls**
  - Play/Pause button
  - Speed slider (0.1x to 10x)
  - Frame-by-frame step
  - Jump to start/end
- **Timeline Scrubber**
  - Horizontal timeline with tick marks
  - Current playback position indicator
  - Event markers (track created, classification change)
  - Minimap showing track trajectory over time
- **Session Info**
  - Session ID, duration
  - Frame count, timestamp range
  - Scene name (if synthetic)
  - Config summary
- **Export**
  - Screenshot (PNG)
  - Record video (WebM)
  - Export track data (CSV/JSON)

#### 6. Bottom Bar: Settings & Toggles
- **Visibility Toggles**
  - Show/hide cameras
  - Show/hide frustums
  - Show/hide rays
  - Show/hide voxels
  - Show/hide measurements
  - Show/hide trails
  - Show/hide covariance
  - Show/hide grid
- **Visual Settings**
  - Voxel score threshold (slider)
  - Trail length (slider)
  - Voxel point size
  - Bloom intensity
  - Camera ray count (sample density)
- **Mode Switchers**
  - Live / Replay / Simulation
  - Abstract Grid / Google Earth
  - Near / Mid / Far scale preset

### Technical Architecture

#### Frontend Stack
```
viz_web/
├── index.html                 # Main HTML shell
├── package.json               # NPM dependencies (if using build)
├── src/
│   ├── main.js                # Bootstrap, initialization
│   ├── renderer/
│   │   ├── scene.js           # Three.js scene, camera, lights
│   │   ├── cameras.js         # Camera node rendering
│   │   ├── frustums.js        # Camera frustum wireframes
│   │   ├── rays.js            # Ray visualization
│   │   ├── tracks.js          # Track spheres, trails, velocity
│   │   ├── voxels.js          # Weavefield point cloud/instances
│   │   ├── measurements.js    # Measurement markers
│   │   ├── grid.js            # World grid, axes
│   │   ├── effects.js         # Bloom, post-processing
│   │   └── labels.js          # CSS2D/3D labels
│   ├── ui/
│   │   ├── sidebar-left.js    # Analytics panel
│   │   ├── sidebar-right.js   # Replay controls
│   │   ├── bottombar.js       # Settings/toggles
│   │   └── hud.js             # Overlay stats
│   ├── data/
│   │   ├── wsclient.js        # WebSocket live stream
│   │   ├── replayer.js        # Replay from recorded session
│   │   ├── state.js           # Global viz state management
│   │   └── decoder.js         # MsgPack/JSON decoding
│   ├── controllers/
│   │   ├── orbit.js           # Orbit controls
│   │   ├── fly.js             # Fly controls (long range)
│   │   └── camera-manager.js  # Camera presets, auto-zoom
│   ├── integrations/
│   │   ├── cesium.js          # CesiumJS Earth integration (optional)
│   │   └── mapbox.js          # Mapbox tiles (optional)
│   └── utils/
│       ├── coords.js          # Coordinate transforms
│       ├── geometry.js        # Geometry utilities
│       └── colors.js          # Color schemes, gradients
├── styles/
│   ├── main.css               # Global styles
│   ├── anduril-theme.css      # Dark theme, neon accents
│   └── panels.css             # Sidebar/panel layouts
└── assets/
    ├── fonts/                 # Monospace fonts
    └── icons/                 # UI icons
```

#### Backend Additions (Python)
```
src/skyweave/viz/
├── server.py                  # Existing aiohttp server
├── frames.py                  # Build VizFrame from state (NEW)
├── replay_api.py              # HTTP API for replay sessions (NEW)
└── session_manager.py         # Manage multiple recording sessions (NEW)
```

**New API Endpoints:**
- `GET /api/sessions` → List available recordings
- `GET /api/sessions/{id}` → Session manifest
- `GET /api/sessions/{id}/frames` → Paginated frame stream
- `GET /api/sessions/{id}/stream` → Real-time replay via WebSocket
- `WS /ws` → Live data stream (existing, enhance)

#### Data Flow

**Live Mode:**
```
Edge/Cameras → MotionPacket → Rayweave → WeavefieldVolume → Track
                                            ↓
                                      VizFrameBuilder
                                            ↓
                                    WebSocket (JSON)
                                            ↓
                                    Three.js Renderer
```

**Replay Mode:**
```
Recorded Session (MsgPack) → Replayer → VizFrame stream
                                          ↓
                                    HTTP/WebSocket
                                          ↓
                                    Three.js Renderer
```

## Implementation Plan

### Phase 1: Core 3D Visualization (Week 1)
**Goal:** Basic Three.js scene with tracks, cameras, voxels

1. **Setup Three.js Scene**
   - Dark background, lighting setup
   - World grid with adaptive scaling
   - OrbitControls for navigation
   - Render loop, stats monitoring

2. **Camera Node Rendering**
   - Parse `VizCamera` from backend
   - Geometric representation (pyramid/cone mesh)
   - Frustum wireframe computation
   - ID labels (CSS2DRenderer)

3. **Track Visualization**
   - Sphere at current position
   - Trail tube/line (THREE.TubeGeometry or custom)
   - Velocity arrow (THREE.ArrowHelper)
   - Basic label (ID, altitude, speed)

4. **Voxel Point Cloud**
   - Parse `WeavefieldVolume.voxels`
   - THREE.Points or InstancedMesh
   - Color by score (shader or vertex colors)
   - Temporal decay (alpha fade)

5. **WebSocket Client**
   - Connect to `ws://localhost:8080/ws`
   - Receive `VizFrame` JSON
   - Update scene state
   - Handle reconnection

**Deliverable:** Working 3D view showing live tracks, cameras, voxels in dark theme

### Phase 2: Anduril Aesthetic & Effects (Week 1-2)
**Goal:** Polish visual style to Anduril standard

1. **Visual Theme**
   - Dark background shader (starfield optional)
   - Neon material palette (cyan, blue, white)
   - Glowing emissive materials for tracks/voxels
   - CSS glass morphism for UI panels

2. **Post-Processing**
   - EffectComposer setup
   - UnrealBloomPass for glow
   - Optional SMAA/FXAA antialiasing
   - Vignette/film grain subtly

3. **Advanced Track Rendering**
   - Covariance ellipsoid (wireframe sphere scaled)
   - Status-based color coding (candidate/active/coasting)
   - Smooth interpolation between updates

4. **Camera Ray Visualization**
   - Optional toggle
   - Sample rays from camera to voxel peak
   - Thin glowing lines (THREE.Line)
   - Intersection point markers

**Deliverable:** Polished Anduril-style visualization

### Phase 3: UI Panels & Analytics (Week 2)
**Goal:** Complete sidebar interfaces

1. **Left Sidebar: Analytics**
   - System status indicators
   - Selected track detail panel
   - Mini graphs (chart.js or canvas)
   - Camera contribution table

2. **Right Sidebar: Replay Controls**
   - Playback buttons (play/pause/step)
   - Timeline scrubber (custom canvas or HTML5 range)
   - Speed slider
   - Session metadata display

3. **Bottom Bar: Settings**
   - Visibility checkboxes
   - Sliders for thresholds
   - Mode switchers (radio buttons)

4. **Responsive Layout**
   - Flexbox/grid layout
   - Collapsible panels
   - Mobile-friendly (optional)

**Deliverable:** Full UI with working controls

### Phase 4: Replay System (Week 2-3)
**Goal:** Load and scrub through recorded sessions

1. **Backend Replay API**
   - `replay_api.py`: HTTP endpoints for sessions
   - Parse MsgPack recordings
   - Stream frames via HTTP chunks or WebSocket
   - Support seek/jump operations

2. **Frontend Replayer**
   - Fetch session list
   - Load session manifest
   - Request frame ranges
   - Implement scrubber seek logic
   - Playback speed control

3. **Timeline Scrubber**
   - Custom canvas-based timeline
   - Event markers (track events)
   - Thumbnail preview on hover (optional)
   - Drag to seek

**Deliverable:** Full replay capability with scrubbing

### Phase 5: Scale & Google Earth (Week 3)
**Goal:** Handle long-range scale, optional Earth overlay

1. **Adaptive Scale System**
   - Detect track distance ranges
   - Switch LOD based on camera distance
   - Adjust grid spacing dynamically
   - Unit conversion (meters/feet/nm)

2. **Google Earth Integration (Option A: Cesium)**
   - Embed CesiumJS
   - Convert world coords to ECEF/WGS84
   - Overlay Three.js scene
   - Sync camera transforms

3. **Google Earth Integration (Option B: Map Tiles)**
   - Fetch Mapbox/Google tiles
   - Project onto ground plane
   - Update tiles on camera move
   - Simpler, no Cesium dependency

4. **Coordinate Systems**
   - Support lat/lon/alt if available
   - ENU (East-North-Up) to world transform
   - Display coordinates in multiple formats

**Deliverable:** Scale handling and Earth overlay option

### Phase 6: Advanced Features (Week 3-4)
**Goal:** Ray visualization, motion profiling, polish

1. **Camera Ray Intersection Visualization**
   - Compute rays from cameras to voxel intersections
   - DDA traversal visualization (show voxel path)
   - Ray-ray closest point (triangulation geometry)
   - Toggle per-camera rays

2. **Motion Profile Analytics**
   - Compute heading, turn rate, acceleration
   - Speed graph over time (sparkline)
   - Behavioral classification hints
   - Altitude profile

3. **Export & Recording**
   - Screenshot capture (PNG)
   - Video recording (MediaRecorder API → WebM)
   - Track data export (JSON/CSV)
   - Session sharing (URL params)

4. **Performance Optimization**
   - Object pooling for voxels
   - LOD for distant objects
   - Frustum culling
   - Worker thread for data processing

**Deliverable:** Complete feature set with polish

### Phase 7: Testing & Documentation (Week 4)
**Goal:** Validate, document, deploy

1. **Testing**
   - Test with synthetic data
   - Test with recorded sessions
   - Test scale transitions
   - Test replay scrubbing
   - Cross-browser testing

2. **Documentation**
   - README for viz_web/
   - Architecture diagram
   - API documentation
   - User guide (controls, features)

3. **Deployment**
   - Build process (Vite/Webpack optional)
   - Static file serving from aiohttp
   - Production config
   - Performance monitoring

**Deliverable:** Production-ready visualizer

## Technical Decisions

### Technology Choices

**Core Rendering:**
- Three.js r160+ (latest stable)
- No build step initially (ES modules from CDN)
- Optional: Vite for development later

**Post-Processing:**
- EffectComposer for bloom/effects
- Custom shaders for voxel rendering

**UI Framework:**
- Vanilla JS + CSS (keep lightweight)
- Optional: Lit for web components if complexity grows

**Data Transport:**
- WebSocket for live streaming
- HTTP/2 + JSON for replay
- MsgPack decoding in browser (msgpack-lite)

**Earth Integration:**
- Phase 1: None (abstract grid only)
- Phase 2: Mapbox tiles on ground plane
- Phase 3: Optional Cesium upgrade

### Coordinate System Strategy
- Backend provides world coords (Z-up, meters)
- Frontend matches Three.js conventions
- Optional transform layer for lat/lon/alt
- Support multiple unit displays

### Performance Targets
- 60fps with 5000 voxels
- <100ms latency for live streaming
- Smooth playback at 10x speed
- Support 10+ simultaneous tracks

### Scalability Strategy
- LOD system kicks in at configurable distances
- Voxel culling based on camera frustum
- Instanced rendering for repeated geometry
- Web Workers for heavy data processing

## Open Questions

1. **Google Earth Preference?**
   - Cesium (photorealistic, complex) vs Mapbox tiles (simple, lightweight)?
   - Does calibration include GPS coordinates?

2. **Deployment Target?**
   - MacBook only or also serve to other devices?
   - Need HTTPS for production?

3. **Data Volume?**
   - Typical recording size/duration?
   - Need server-side downsampling for large sessions?

4. **Ray Visualization Density?**
   - Show all rays or sample subset?
   - Per-voxel or per-detection?

5. **Motion Profile Scope?**
   - Which specific metrics are most valuable?
   - Need historical comparison (current vs previous tracks)?

## Success Criteria

✅ **Visual Quality:** Matches Anduril aesthetic (dark, neon, glowing, tactical)
✅ **Scale Handling:** Smooth visualization from 1m to 6000m (20k ft)
✅ **Live Mode:** Real-time display with <300ms latency
✅ **Replay Mode:** Scrubbing through recorded sessions
✅ **Analytics:** Rich track and system data in sidebars
✅ **Ray Visualization:** Optional camera-to-voxel ray display
✅ **Performance:** 60fps with typical data loads
✅ **Earth Integration:** At least ground plane with optional satellite imagery

## Next Steps

1. Review plan with user
2. Confirm Google Earth integration approach
3. Confirm deployment requirements
4. Begin Phase 1 implementation
