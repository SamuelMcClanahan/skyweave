// Tooltip manager for hover information
export class TooltipManager {
    constructor(state) {
        this.state = state;
        this.tooltip = null;
        this.hoveredObject = null;
        this.isDragging = false;
        this.dragOffset = { x: 0, y: 0 };
        this.tooltipPosition = null; // Store custom position when dragged
        this.createTooltip();
    }

    createTooltip() {
        this.tooltip = document.createElement('div');
        this.tooltip.id = 'hover-tooltip';
        this.tooltip.style.position = 'fixed';
        this.tooltip.style.background = 'rgba(0, 0, 0, 0.92)';
        this.tooltip.style.color = '#ffffff';
        this.tooltip.style.padding = '10px 14px';
        this.tooltip.style.borderRadius = '6px';
        this.tooltip.style.fontSize = '13px';
        this.tooltip.style.fontFamily = 'Inter, sans-serif';
        this.tooltip.style.pointerEvents = 'auto'; // Changed to auto for dragging
        this.tooltip.style.zIndex = '10000';
        this.tooltip.style.display = 'none';
        this.tooltip.style.maxWidth = '320px';
        this.tooltip.style.minWidth = '240px';
        this.tooltip.style.border = '1px solid rgba(0, 229, 255, 0.3)';
        this.tooltip.style.boxShadow = '0 4px 16px rgba(0, 0, 0, 0.6)';
        this.tooltip.style.cursor = 'move';
        this.tooltip.style.userSelect = 'none';

        // Add drag header
        const header = document.createElement('div');
        header.style.marginBottom = '8px';
        header.style.paddingBottom = '6px';
        header.style.borderBottom = '1px solid rgba(255, 255, 255, 0.1)';
        header.style.fontSize = '11px';
        header.style.color = '#888';
        header.style.textAlign = 'right';
        header.innerHTML = '⋮⋮ drag to move';
        this.tooltip.appendChild(header);

        this.contentDiv = document.createElement('div');
        this.tooltip.appendChild(this.contentDiv);

        document.body.appendChild(this.tooltip);

        // Setup drag handlers
        this.tooltip.addEventListener('mousedown', this.onDragStart.bind(this));
        document.addEventListener('mousemove', this.onDrag.bind(this));
        document.addEventListener('mouseup', this.onDragEnd.bind(this));
    }

    onDragStart(e) {
        if (e.target === this.tooltip || e.target.parentElement === this.tooltip) {
            this.isDragging = true;
            const rect = this.tooltip.getBoundingClientRect();
            this.dragOffset.x = e.clientX - rect.left;
            this.dragOffset.y = e.clientY - rect.top;
            e.preventDefault();
        }
    }

    onDrag(e) {
        if (this.isDragging) {
            const x = e.clientX - this.dragOffset.x;
            const y = e.clientY - this.dragOffset.y;

            // Keep within viewport
            const rect = this.tooltip.getBoundingClientRect();
            const maxX = window.innerWidth - rect.width;
            const maxY = window.innerHeight - rect.height;

            this.tooltipPosition = {
                x: Math.max(0, Math.min(x, maxX)),
                y: Math.max(0, Math.min(y, maxY))
            };

            this.tooltip.style.left = `${this.tooltipPosition.x}px`;
            this.tooltip.style.top = `${this.tooltipPosition.y}px`;

            e.preventDefault();
        }
    }

    onDragEnd(e) {
        if (this.isDragging) {
            this.isDragging = false;
            e.preventDefault();
        }
    }

    show(content, x, y) {
        this.contentDiv.innerHTML = content;
        this.tooltip.style.display = 'block';

        // If user has dragged to a custom position, keep it there
        if (this.tooltipPosition) {
            this.tooltip.style.left = `${this.tooltipPosition.x}px`;
            this.tooltip.style.top = `${this.tooltipPosition.y}px`;
            return;
        }

        // Otherwise follow cursor with offset
        const offsetX = 20;
        const offsetY = 20;

        // Keep within viewport bounds
        const rect = this.tooltip.getBoundingClientRect();
        let finalX = x + offsetX;
        let finalY = y + offsetY;

        if (finalX + rect.width > window.innerWidth) {
            finalX = x - rect.width - offsetX;
        }
        if (finalY + rect.height > window.innerHeight) {
            finalY = y - rect.height - offsetY;
        }

        this.tooltip.style.left = `${finalX}px`;
        this.tooltip.style.top = `${finalY}px`;
    }

    hide() {
        this.tooltip.style.display = 'none';
        this.hoveredObject = null;
        this.tooltipPosition = null; // Reset custom position when hiding
    }

    update(intersectedObject, mouseX, mouseY) {
        if (!intersectedObject) {
            this.hide();
            return;
        }

        // Generate content based on object type
        let content = '';

        if (intersectedObject.trackId !== undefined) {
            // Track hover
            const track = this.state.tracks.get(intersectedObject.trackId);
            if (track) {
                content = this.formatTrackTooltip(track);
            }
        } else if (intersectedObject.cameraId !== undefined) {
            // Camera hover
            const camera = this.state.cameras.get(intersectedObject.cameraId);
            if (camera) {
                content = this.formatCameraTooltip(camera);
            }
        }

        if (content) {
            this.show(content, mouseX, mouseY);
            this.hoveredObject = intersectedObject;
        } else {
            this.hide();
        }
    }

    formatTrackTooltip(track) {
        const pos = track.state.slice(0, 3);
        const vel = track.state.slice(3, 6);
        const speed = Math.sqrt(vel[0]**2 + vel[1]**2 + vel[2]**2);
        const supportCount = (track.visible_camera_ids || []).length;
        const totalCameras = this.state.cameras.size;

        let quality = 'None';
        if (track.status === 'coasting') {
            quality = 'Coasting';
        } else if (supportCount >= 4) {
            quality = '<span style="color: #00ff88">Strong</span>';
        } else if (supportCount === 3) {
            quality = '<span style="color: #00ccff">Moderate</span>';
        } else if (supportCount >= 2) {
            quality = '<span style="color: #ffcc00">Weak</span>';
        }

        return `
            <div style="font-weight: bold; margin-bottom: 6px; color: #00e5ff">Track #${track.id}</div>
            <div style="margin-bottom: 4px">
                <span style="color: #aaa">Class:</span> ${track.classification || 'unknown'}
                <span style="margin-left: 8px; color: #aaa">Status:</span> ${track.status}
            </div>
            <div style="margin-bottom: 4px">
                <span style="color: #aaa">Speed:</span> ${speed.toFixed(1)} m/s (${(speed * 2.237).toFixed(1)} mph)
            </div>
            <div style="margin-bottom: 4px">
                <span style="color: #aaa">Altitude:</span> ${pos[2].toFixed(1)} m
            </div>
            <div>
                <span style="color: #aaa">Support:</span> ${supportCount}/${totalCameras} cameras (${quality})
            </div>
        `;
    }

    formatCameraTooltip(camera) {
        const status = camera.online ? '<span style="color: #00ff88">Online</span>' : '<span style="color: #ff4400">Offline</span>';
        const pos = camera.position;

        let stats = '';
        if (camera.fps_actual !== undefined) {
            stats += `<div style="margin-bottom: 4px">
                <span style="color: #aaa">FPS:</span> ${camera.fps_actual.toFixed(1)}
            </div>`;
        }
        if (camera.latency_ms !== undefined) {
            stats += `<div style="margin-bottom: 4px">
                <span style="color: #aaa">Latency:</span> ${Math.round(camera.latency_ms)}ms
            </div>`;
        }

        return `
            <div style="font-weight: bold; margin-bottom: 6px; color: #00e5ff">Camera ${camera.id}</div>
            <div style="margin-bottom: 4px">
                <span style="color: #aaa">Status:</span> ${status}
            </div>
            <div style="margin-bottom: 4px">
                <span style="color: #aaa">FOV:</span> ${camera.fov_h_deg.toFixed(1)}° × ${camera.fov_v_deg.toFixed(1)}°
            </div>
            <div style="margin-bottom: 4px">
                <span style="color: #aaa">Range:</span> ${(camera.max_range_m / 1000).toFixed(1)} km
            </div>
            ${stats}
            <div style="margin-top: 6px; font-size: 11px; color: #888">
                Click to select
            </div>
        `;
    }
}
