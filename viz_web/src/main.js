// Main application entry point
import { SceneManager } from './renderer/scene.js';
import { WSClient } from './data/wsclient.js';
import { UIManager } from './ui/ui-manager.js';
import { VizState } from './data/state.js';

class SkyweaveViz {
    constructor() {
        this.state = new VizState();
        this.sceneManager = null;
        this.wsClient = null;
        this.uiManager = null;

        this.stats = {
            fps: 0,
            frameCount: 0,
            lastTime: performance.now(),
            messageCount: 0
        };
    }

    async init() {
        console.log('Initializing Skyweave Visualizer...');

        try {
            // Initialize scene manager (Three.js)
            this.sceneManager = new SceneManager(this.state);
            await this.sceneManager.init();

            // Initialize UI manager
            this.uiManager = new UIManager(this.state, this.sceneManager);
            this.uiManager.init();

            // Initialize WebSocket client
            this.wsClient = new WSClient(this.state);
            this.wsClient.onMessage = this.handleMessage.bind(this);
            this.wsClient.onConnectionChange = this.handleConnectionChange.bind(this);

            // Connect to backend
            await this.wsClient.connect();

            // Start render loop
            this.animate();

            console.log('Skyweave Visualizer initialized successfully');
        } catch (error) {
            console.error('Failed to initialize visualizer:', error);
            this.showError('Initialization failed: ' + error.message);
        }
    }

    handleMessage(data) {
        this.stats.messageCount++;

        // Update state from VizFrame
        if (data.tracks) {
            this.state.updateTracks(data.tracks);
        }
        if (data.cameras) {
            this.state.updateCameras(data.cameras);
        }
        if (data.weavefield_history) {
            this.state.updateWeavefield(data.weavefield_history);
        }
        if (data.measurements) {
            this.state.updateMeasurements(data.measurements);
        }
        if (data.stats) {
            this.state.updateStats(data.stats);
        }

        // Update UI
        this.uiManager.update();
    }

    handleConnectionChange(connected) {
        const indicator = document.getElementById('connection-indicator');
        const status = document.getElementById('connection-status');
        const wsStatus = document.getElementById('ws-status');

        if (connected) {
            indicator.className = 'status-indicator online';
            status.textContent = 'CONNECTED';
            wsStatus.textContent = 'Connected';
        } else {
            indicator.className = 'status-indicator connecting';
            status.textContent = 'CONNECTING';
            wsStatus.textContent = 'Connecting...';
        }
    }

    animate() {
        requestAnimationFrame(() => this.animate());

        // Update FPS counter
        const now = performance.now();
        this.stats.frameCount++;
        const elapsed = now - this.stats.lastTime;

        if (elapsed >= 1000) {
            this.stats.fps = Math.round((this.stats.frameCount * 1000) / elapsed);
            this.stats.frameCount = 0;
            this.stats.lastTime = now;

            document.getElementById('fps-display').textContent = this.stats.fps;
            document.getElementById('msg-count').textContent = this.stats.messageCount;
        }

        // Render scene
        if (this.sceneManager) {
            this.sceneManager.render();
        }
    }

    showError(message) {
        // Simple error display - could be enhanced with modal
        console.error(message);
        alert('Error: ' + message);
    }
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        const app = new SkyweaveViz();
        app.init();
    });
} else {
    const app = new SkyweaveViz();
    app.init();
}

export { SkyweaveViz };
