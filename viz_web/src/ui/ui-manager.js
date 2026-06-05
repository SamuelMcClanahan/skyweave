// UI Manager - handles all UI interactions and updates
export class UIManager {
    constructor(state, sceneManager) {
        this.state = state;
        this.sceneManager = sceneManager;

        this.elements = {};
    }

    init() {
        // Cache DOM elements
        this.cacheElements();

        // Setup event listeners
        this.setupEventListeners();

        // Subscribe to state changes
        this.state.subscribe(this.onStateChange.bind(this));

        requestAnimationFrame(() => {
            document.body.classList.add('ui-ready');
        });
    }

    cacheElements() {
        // Top bar
        this.elements.trackCount = document.getElementById('track-count');
        this.elements.fpsDisplay = document.getElementById('fps-display');
        this.elements.latencyDisplay = document.getElementById('latency-display');

        // Left sidebar
        this.elements.camerasOnline = document.getElementById('cameras-online');
        this.elements.activeTracks = document.getElementById('active-tracks');
        this.elements.processingRate = document.getElementById('processing-rate');
        this.elements.voxelCount = document.getElementById('voxel-count');
        this.elements.trackList = document.getElementById('track-list');
        this.elements.cameraList = document.getElementById('camera-list');

        // Right sidebar
        this.elements.scaleSlider = document.getElementById('scale-slider');
        this.elements.autoZoom = document.getElementById('auto-zoom');
        this.elements.rayModeRadios = document.querySelectorAll('input[name="ray-mode"]');
        this.elements.perCameraRays = document.getElementById('per-camera-rays');

        // Bottom bar toggles
        this.elements.toggleCameras = document.getElementById('toggle-cameras');
        this.elements.toggleFrustums = document.getElementById('toggle-frustums');
        this.elements.toggleVoxels = document.getElementById('toggle-voxels');
        this.elements.toggleTracks = document.getElementById('toggle-tracks');
        this.elements.toggleTrails = document.getElementById('toggle-trails');
        this.elements.toggleGrid = document.getElementById('toggle-grid');

        // Floating panel
        this.elements.trackDetailPanel = document.getElementById('track-detail-panel');
        this.elements.panelClose = document.getElementById('panel-close');
        this.elements.btnFollowTrack = document.getElementById('btn-follow-track');
        this.elements.btnCenterTrack = document.getElementById('btn-center-track');

        // Buttons
        this.elements.btnResetCamera = document.getElementById('btn-reset-camera');
        this.elements.btnFitAll = document.getElementById('btn-fit-all');
    }

    setupEventListeners() {
        // Visibility toggles
        this.elements.toggleCameras.addEventListener('change', (e) => {
            this.state.setVisibility('cameras', e.target.checked);
        });

        this.elements.toggleFrustums.addEventListener('change', (e) => {
            this.state.setVisibility('frustums', e.target.checked);
        });

        this.elements.toggleVoxels.addEventListener('change', (e) => {
            this.state.setVisibility('voxels', e.target.checked);
        });

        this.elements.toggleTracks.addEventListener('change', (e) => {
            this.state.setVisibility('tracks', e.target.checked);
        });

        this.elements.toggleTrails.addEventListener('change', (e) => {
            this.state.setVisibility('trails', e.target.checked);
        });

        this.elements.toggleGrid.addEventListener('change', (e) => {
            this.state.setVisibility('grid', e.target.checked);
        });

        // Ray mode toggles
        this.elements.rayModeRadios.forEach(radio => {
            radio.addEventListener('change', (e) => {
                if (e.target.checked) {
                    this.state.setVisibility('rays', e.target.value);
                }
            });
        });

        // Settings
        this.elements.scaleSlider.addEventListener('input', (e) => {
            this.state.setSetting('scaleValue', parseInt(e.target.value));
        });

        this.elements.autoZoom.addEventListener('change', (e) => {
            this.state.setSetting('autoZoom', e.target.checked);
        });

        // Panel controls
        this.elements.panelClose.addEventListener('click', () => {
            this.hideTrackDetailPanel();
            this.state.selectTrack(null);
        });

        this.elements.btnFollowTrack.addEventListener('click', () => {
            const trackId = this.state.selectedTrackId;
            if (trackId) {
                this.state.followTrack(trackId);
            }
        });

        this.elements.btnCenterTrack.addEventListener('click', () => {
            const track = this.state.getActiveTrack();
            if (track) {
                this.sceneManager.centerOnTrack(track);
            }
        });

        // Camera controls
        this.elements.btnResetCamera.addEventListener('click', () => {
            this.sceneManager.resetCamera();
        });

        this.elements.btnFitAll.addEventListener('click', () => {
            this.sceneManager.fitToTracks();
        });
    }

    onStateChange(event, state) {
        switch (event) {
            case 'tracks':
                this.updateTrackList();
                this.updateSystemStatus();
                if (state.selectedTrackId) {
                    this.updateTrackDetailPanel();
                }
                break;

            case 'cameras':
                this.updateCameraList();
                this.updateSystemStatus();
                this.updateRayControls();
                break;

            case 'stats':
                this.updateSystemStatus();
                break;

            case 'selection':
                if (state.selectedTrackId) {
                    this.showTrackDetailPanel();
                    this.updateTrackDetailPanel();
                } else {
                    this.hideTrackDetailPanel();
                }
                break;

            case 'follow':
                this.updateFollowButton();
                break;

            case 'settings':
                this.updateSettingsControls();
                break;
        }
    }

    update() {
        // Called on every message from backend
        this.updateSystemStatus();
        this.updateTrackList();
        this.updateCameraList();

        if (this.state.selectedTrackId) {
            this.updateTrackDetailPanel();
        }
    }

    updateSystemStatus() {
        // Update top bar
        this.elements.trackCount.textContent = this.state.tracks.size;

        // Update left sidebar stats
        const onlineCameras = Array.from(this.state.cameras.values()).filter(c => c.online).length;
        this.elements.camerasOnline.textContent = `${onlineCameras}/${this.state.cameras.size}`;

        const activeTracks = Array.from(this.state.tracks.values()).filter(t => t.status === 'active').length;
        this.elements.activeTracks.textContent = activeTracks;

        if (this.state.stats.fps) {
            this.elements.processingRate.textContent = `${this.state.stats.fps.toFixed(1)} Hz`;
        }

        // Count voxels
        let totalVoxels = 0;
        this.state.weavefieldHistory.forEach(volume => {
            if (volume.voxels) {
                totalVoxels += volume.voxels.length;
            }
        });
        this.elements.voxelCount.textContent = totalVoxels;

        // Update latency display
        if (this.state.stats.latency_p50_ms) {
            this.elements.latencyDisplay.textContent = `${Math.round(this.state.stats.latency_p50_ms)}ms`;
        }
    }

    updateTrackList() {
        const container = this.elements.trackList;

        if (this.state.tracks.size === 0) {
            container.innerHTML = '<div class="empty-state">No active tracks</div>';
            return;
        }

        container.innerHTML = '';

        this.state.tracks.forEach(track => {
            const item = this.createTrackListItem(track);
            container.appendChild(item);
        });
    }

    createTrackListItem(track) {
        const item = document.createElement('div');
        item.className = 'track-item';
        if (track.id === this.state.selectedTrackId) {
            item.className += ' selected';
        }

        const speed = Math.sqrt(track.state[3]**2 + track.state[4]**2 + track.state[5]**2);
        const altitude = track.state[2];

        const classificationClass = track.classification || 'unknown';

        item.innerHTML = `
            <div class="track-header">
                <span class="track-id">Track #${track.id}</span>
                <span class="track-classification ${classificationClass}">${classificationClass}</span>
            </div>
            <div class="track-stats">
                <div class="track-stat">Alt: <span class="track-stat-value">${altitude.toFixed(1)}m</span></div>
                <div class="track-stat">Speed: <span class="track-stat-value">${speed.toFixed(1)}m/s</span></div>
            </div>
        `;

        item.addEventListener('click', () => {
            this.state.selectTrack(track.id);
        });

        return item;
    }

    updateCameraList() {
        const container = this.elements.cameraList;

        if (this.state.cameras.size === 0) {
            container.innerHTML = '<div class="empty-state">No cameras</div>';
            return;
        }

        container.innerHTML = '';

        this.state.cameras.forEach(camera => {
            const item = this.createCameraListItem(camera);
            container.appendChild(item);
        });
    }

    createCameraListItem(camera) {
        const item = document.createElement('div');
        item.className = 'camera-item';

        const statusClass = camera.online ? 'online' : 'offline';
        const statusText = camera.online ? 'Online' : 'Offline';

        // Format FPS and latency if available
        const fpsText = camera.fps_actual ? `${camera.fps_actual.toFixed(1)} FPS` : '';
        const latencyText = camera.latency_ms ? `${Math.round(camera.latency_ms)}ms` : '';
        const motionText = camera.motion_pixel_count ? `${(camera.motion_pixel_count / 1000).toFixed(1)}k px` : '';

        const statsLine = [fpsText, latencyText, motionText].filter(s => s).join(' | ');

        item.innerHTML = `
            <div class="camera-header">
                <span class="camera-name">Camera ${camera.id}</span>
                <div class="camera-status">
                    <span class="status-indicator ${statusClass}"></span>
                    <span>${statusText}</span>
                </div>
            </div>
            ${statsLine ? `<div class="camera-stats">${statsLine}</div>` : ''}
        `;

        return item;
    }

    updateRayControls() {
        const container = this.elements.perCameraRays;
        container.innerHTML = '';

        this.state.cameras.forEach(camera => {
            const label = document.createElement('label');
            label.className = 'control-label';

            const radio = document.createElement('input');
            radio.type = 'radio';
            radio.name = 'ray-mode';
            radio.value = `camera_${camera.id}`;

            radio.addEventListener('change', (e) => {
                if (e.target.checked) {
                    this.state.setVisibility('rays', e.target.value);
                }
            });

            label.appendChild(radio);
            label.appendChild(document.createTextNode(` Camera ${camera.id} Only`));
            container.appendChild(label);
        });
    }

    updateSettingsControls() {
        if (this.elements.scaleSlider) {
            this.elements.scaleSlider.value = this.state.settings.scaleValue;
        }
        if (this.elements.autoZoom) {
            this.elements.autoZoom.checked = this.state.settings.autoZoom;
        }
    }

    showTrackDetailPanel() {
        const panel = this.elements.trackDetailPanel;
        if (panel.classList.contains('visible')) {
            return;
        }
        panel.style.display = 'block';
        requestAnimationFrame(() => {
            panel.classList.add('visible');
        });
    }

    hideTrackDetailPanel() {
        const panel = this.elements.trackDetailPanel;
        panel.classList.remove('visible');
        window.setTimeout(() => {
            if (!panel.classList.contains('visible')) {
                panel.style.display = 'none';
            }
        }, 180);
    }

    updateTrackDetailPanel() {
        const track = this.state.getActiveTrack();
        if (!track) {
            this.hideTrackDetailPanel();
            return;
        }

        // Update panel title
        document.getElementById('panel-track-title').textContent = `Track #${track.id} - ${track.status.toUpperCase()}`;

        // Classification
        document.getElementById('detail-classification').textContent = track.classification || 'Unknown';
        document.getElementById('detail-confidence').textContent = track.classification_confidence
            ? `${(track.classification_confidence * 100).toFixed(0)}%`
            : '--';

        // Position and kinematics
        const pos = track.state.slice(0, 3);
        document.getElementById('detail-position').textContent = `${pos[0].toFixed(2)}, ${pos[1].toFixed(2)}, ${pos[2].toFixed(2)}`;
        document.getElementById('detail-altitude').textContent = `${pos[2].toFixed(2)} m`;

        const velocity = track.state.slice(3, 6);
        const speed = Math.sqrt(velocity[0]**2 + velocity[1]**2 + velocity[2]**2);
        document.getElementById('detail-speed').textContent = `${speed.toFixed(2)} m/s (${(speed * 2.237).toFixed(1)} mph)`;

        // Heading (0-360 degrees from North)
        const heading = (Math.atan2(velocity[0], velocity[1]) * 180 / Math.PI + 360) % 360;
        const headingDir = this.getHeadingDirection(heading);
        document.getElementById('detail-heading').textContent = `${heading.toFixed(0)}° (${headingDir})`;

        // Climb rate
        document.getElementById('detail-climb-rate').textContent = `${velocity[2].toFixed(2)} m/s`;

        // Acceleration (would need previous velocity to calculate)
        document.getElementById('detail-acceleration').textContent = '-- m/s²';

        // Track metadata
        const age = (Date.now() / 1000) - (track.created_ts_ns / 1_000_000_000);
        document.getElementById('detail-age').textContent = `${age.toFixed(1)} s`;
        document.getElementById('detail-updates').textContent = track.update_count;

        // Camera support
        const cameraIds = track.visible_camera_ids || [];
        const totalCameras = this.state.cameras.size;
        const supportCount = cameraIds.length;

        // Determine support quality
        let supportQuality = 'None';
        if (track.status === 'coasting') {
            supportQuality = 'Coasting';
        } else if (supportCount >= 4) {
            supportQuality = 'Strong';
        } else if (supportCount === 3) {
            supportQuality = 'Moderate';
        } else if (supportCount >= 2) {
            supportQuality = 'Weak';
        }

        document.getElementById('detail-cameras').textContent = supportCount > 0
            ? `${supportCount}/${totalCameras} cameras (${supportQuality}) - ${cameraIds.map(id => `CAM ${id}`).join(', ')}`
            : 'None';
    }

    updateFollowButton() {
        const btn = this.elements.btnFollowTrack;
        if (this.state.followingTrackId === this.state.selectedTrackId) {
            btn.textContent = 'Unfollow Camera';
            btn.onclick = () => this.state.followTrack(null);
        } else {
            btn.textContent = 'Follow Camera';
            btn.onclick = () => this.state.followTrack(this.state.selectedTrackId);
        }
    }

    getHeadingDirection(degrees) {
        const directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
        const index = Math.round(degrees / 22.5) % 16;
        return directions[index];
    }
}
