// Scene manager: Cesium earth context plus a transparent Three.js overlay.
import * as THREE from 'three';

import { CameraRenderer } from './cameras.js';
import { TrackRenderer } from './tracks.js';
import { VoxelRenderer } from './voxels.js';
import { RayRenderer } from './rays.js';
import { GridRenderer } from './grid.js';
import { FovCoverageRenderer } from './fov-coverage.js';
import { TooltipManager } from '../ui/tooltip-manager.js';

const OSM_TILE_URL = 'https://tile.openstreetmap.org/';
const OSM_CREDIT = 'Map data (c) OpenStreetMap contributors';
const EARTH_FALLBACK_COLOR = '#10131a';
const AUTO_FIT_INTERVAL_MS = 1500;
const SCALE_MIN_RANGE_M = 50;
const SCALE_MAX_RANGE_M = 10000;
const DEFAULT_VIEW_PITCH_RAD = -Math.PI / 4;
const POINTER_DRAG_THRESHOLD_PX = 6;
const DRAG_CLICK_SUPPRESS_MS = 250;
const BASE_LAYER_DARK_STYLE = {
    brightness: 0.48,
    contrast: 1.35,
    saturation: 0.35,
    gamma: 0.85
};

export class SceneManager {
    constructor(state) {
        this.state = state;

        // Cesium viewer
        this.cesiumViewer = null;

        // Three.js components
        this.scene = null;
        this.camera = null;
        this.renderer = null;
        this.controls = null;

        // Renderers for different object types
        this.cameraRenderer = null;
        this.trackRenderer = null;
        this.voxelRenderer = null;
        this.rayRenderer = null;
        this.gridRenderer = null;
        this.fovCoverageRenderer = null;

        // Raycaster for mouse interaction
        this.raycaster = new THREE.Raycaster();
        this.mouse = new THREE.Vector2();

        // Tooltip manager
        this.tooltipManager = null;

        this.canvas = null;
        this.lastAutoFitMs = 0;
        this.lastAppliedScaleValue = state.settings.scaleValue;
        this.pointerDown = null;
        this.pointerDragged = false;
        this.lastDragEndMs = 0;
        this.hoveredCameraId = null;
    }

    async init() {
        this.canvas = document.getElementById('three-canvas');

        // Initialize Cesium first as the earth layer.
        this.initCesium();

        // Initialize Three.js as the transparent tactical overlay.
        this.initThree();

        // Initialize renderers
        this.cameraRenderer = new CameraRenderer(this.scene);
        this.trackRenderer = new TrackRenderer(this.scene);
        this.voxelRenderer = new VoxelRenderer(this.scene);
        this.rayRenderer = new RayRenderer(this.scene);
        this.gridRenderer = new GridRenderer(this.scene);
        this.fovCoverageRenderer = new FovCoverageRenderer(this.scene);
        this.tooltipManager = new TooltipManager(this.state);
        this.applySettings(this.state.settings, true);

        // Setup event listeners
        this.setupEventListeners();

        // Subscribe to state changes
        this.state.subscribe(this.onStateChange.bind(this));
    }

    initCesium() {
        this.configureCesiumIon();

        // Initialize Cesium viewer focused on San Francisco.
        const viewerOptions = {
            baseLayerPicker: false,
            geocoder: false,
            homeButton: false,
            sceneModePicker: false,
            navigationHelpButton: false,
            animation: false,
            timeline: false,
            fullscreenButton: false,
            vrButton: false,
            infoBox: false,
            selectionIndicator: false,
            shadows: false
        };
        this.addTerrainOption(viewerOptions);
        this.addImageryOption(viewerOptions);

        try {
            this.cesiumViewer = new Cesium.Viewer('cesiumContainer', viewerOptions);
        } catch (error) {
            console.warn('Cesium terrain or imagery failed; falling back to plain globe.', error);
            delete viewerOptions.terrain;
            delete viewerOptions.terrainProvider;
            delete viewerOptions.baseLayer;
            delete viewerOptions.imageryProvider;
            this.cesiumViewer = new Cesium.Viewer('cesiumContainer', viewerOptions);
        }
        this.installImageryDiagnostics();
        this.installManualNavigationHandlers();

        // Define local origin for our tracking coordinate system.
        // This is the reference point: our local (0,0,0) corresponds to this lat/lon
        this.localOriginLongitude = -122.4194;
        this.localOriginLatitude = 37.7749;
        this.localOriginCartesian = Cesium.Cartesian3.fromDegrees(
            this.localOriginLongitude,
            this.localOriginLatitude,
            0
        );

        // ENU maps Skyweave local meters to Cesium earth coordinates.
        this.enuTransform = Cesium.Transforms.eastNorthUpToFixedFrame(this.localOriginCartesian);
        this.enuTransformInverse = Cesium.Matrix4.inverse(this.enuTransform, new Cesium.Matrix4());

        // Set initial view to San Francisco downtown
        this.cesiumViewer.camera.setView({
            destination: Cesium.Cartesian3.fromDegrees(-122.4194, 37.7749, 2000),
            orientation: {
                heading: Cesium.Math.toRadians(0),
                pitch: Cesium.Math.toRadians(-45),
                roll: 0.0
            }
        });

        // Disable Cesium's default atmosphere effects for cleaner look
        if (this.cesiumViewer.scene.skyAtmosphere) {
            this.cesiumViewer.scene.skyAtmosphere.show = false;
        }
        if (this.cesiumViewer.scene.sun) {
            this.cesiumViewer.scene.sun.show = false;
        }
        if (this.cesiumViewer.scene.moon) {
            this.cesiumViewer.scene.moon.show = false;
        }
        this.cesiumViewer.scene.backgroundColor = Cesium.Color.BLACK;
        if (this.cesiumViewer.scene.globe) {
            this.cesiumViewer.scene.globe.baseColor = Cesium.Color.fromCssColorString(EARTH_FALLBACK_COLOR);
        }
    }

    configureCesiumIon() {
        const token = window.SKYWEAVE_CESIUM_ION_TOKEN || window.CESIUM_ION_TOKEN;
        if (token && Cesium.Ion) {
            Cesium.Ion.defaultAccessToken = token;
        }
    }

    hasCesiumIonToken() {
        return Boolean(window.SKYWEAVE_CESIUM_ION_TOKEN || window.CESIUM_ION_TOKEN);
    }

    addTerrainOption(viewerOptions) {
        if (!this.hasCesiumIonToken()) {
            return;
        }
        if (Cesium.Terrain?.fromWorldTerrain) {
            viewerOptions.terrain = Cesium.Terrain.fromWorldTerrain();
        } else if (Cesium.createWorldTerrain) {
            viewerOptions.terrainProvider = Cesium.createWorldTerrain();
        }
    }

    addImageryOption(viewerOptions) {
        const provider = this.createOsmImageryProvider();
        if (!provider) {
            return;
        }

        if (Cesium.ImageryLayer) {
            viewerOptions.baseLayer = new Cesium.ImageryLayer(provider);
        } else {
            viewerOptions.imageryProvider = provider;
        }
    }

    createOsmImageryProvider() {
        if (Cesium.OpenStreetMapImageryProvider) {
            return new Cesium.OpenStreetMapImageryProvider({
                url: OSM_TILE_URL,
                credit: OSM_CREDIT,
                maximumLevel: 19
            });
        }
        if (Cesium.UrlTemplateImageryProvider) {
            return new Cesium.UrlTemplateImageryProvider({
                url: `${OSM_TILE_URL}{z}/{x}/{y}.png`,
                credit: OSM_CREDIT,
                maximumLevel: 19,
                enablePickFeatures: false
            });
        }
        return null;
    }

    installImageryDiagnostics() {
        const layers = this.cesiumViewer.scene.imageryLayers;
        if (!layers || layers.length === 0) {
            console.warn('Cesium initialized without a base imagery layer.');
            return;
        }

        const baseLayer = layers.get(0);
        const provider = baseLayer?.imageryProvider;
        console.info(`Cesium base imagery: ${provider?.constructor?.name || 'unknown'}`);
        provider?.errorEvent?.addEventListener(error => {
            console.warn('Cesium imagery tile error', error);
        });
        baseLayer.show = true;
        baseLayer.alpha = 1.0;
        baseLayer.brightness = BASE_LAYER_DARK_STYLE.brightness;
        baseLayer.contrast = BASE_LAYER_DARK_STYLE.contrast;
        baseLayer.saturation = BASE_LAYER_DARK_STYLE.saturation;
        baseLayer.gamma = BASE_LAYER_DARK_STYLE.gamma;
    }

    installManualNavigationHandlers() {
        const canvas = this.cesiumViewer.scene?.canvas;
        if (!canvas) {
            return;
        }

        const disableAutoZoom = () => {
            this.cesiumViewer.camera.cancelFlight?.();
            if (this.state.settings.autoZoom) {
                this.state.setSetting('autoZoom', false);
            }
        };

        canvas.addEventListener('pointerdown', event => {
            this.pointerDown = { x: event.clientX, y: event.clientY };
            this.pointerDragged = false;
        }, { passive: true });
        canvas.addEventListener('pointermove', event => {
            if (!this.pointerDown) {
                return;
            }
            const dx = event.clientX - this.pointerDown.x;
            const dy = event.clientY - this.pointerDown.y;
            if (Math.hypot(dx, dy) >= POINTER_DRAG_THRESHOLD_PX) {
                this.pointerDragged = true;
                disableAutoZoom();
            }
        }, { passive: true });
        canvas.addEventListener('pointerup', () => {
            if (this.pointerDragged) {
                this.lastDragEndMs = performance.now();
            }
            this.pointerDown = null;
        }, { passive: true });
        canvas.addEventListener('wheel', disableAutoZoom, { passive: true });
        canvas.addEventListener('touchstart', disableAutoZoom, { passive: true });
    }

    initThree() {
        // Create scene with a transparent background to overlay on Cesium.
        this.scene = new THREE.Scene();

        // Create camera (will sync with Cesium camera each frame)
        const width = this.canvas.clientWidth;
        const height = this.canvas.clientHeight;
        this.camera = new THREE.PerspectiveCamera(60, width / height, 0.1, 100000);

        // Create renderer with transparency
        this.renderer = new THREE.WebGLRenderer({
            canvas: this.canvas,
            alpha: true,
            antialias: true
        });
        this.renderer.setSize(width, height);
        this.renderer.setPixelRatio(window.devicePixelRatio);
        this.renderer.setClearColor(0x000000, 0); // Fully transparent
        this.renderer.autoClear = true;

        // No OrbitControls - Cesium handles camera navigation
        // Three.js camera will sync to Cesium's camera position

        // Lighting
        const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
        this.scene.add(ambientLight);

        const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
        directionalLight.position.set(50, 50, 50);
        this.scene.add(directionalLight);
    }

    syncCameraWithCesium() {
        // Sync Three.js camera with Cesium camera for seamless overlay
        const cesiumCamera = this.cesiumViewer.camera;

        // Get Cesium camera position (in ECEF coordinates)
        const cesiumPositionECEF = cesiumCamera.positionWC;
        const cesiumDirection = cesiumCamera.directionWC;
        const cesiumUp = cesiumCamera.upWC;

        // Convert ECEF position to local ENU coordinates
        const localPosition = new Cesium.Cartesian3();
        Cesium.Matrix4.multiplyByPoint(this.enuTransformInverse, cesiumPositionECEF, localPosition);

        // Convert ECEF direction vector to local ENU
        const targetECEF = Cesium.Cartesian3.add(cesiumPositionECEF, cesiumDirection, new Cesium.Cartesian3());
        const targetLocal = new Cesium.Cartesian3();
        Cesium.Matrix4.multiplyByPoint(this.enuTransformInverse, targetECEF, targetLocal);

        // Convert ECEF up vector to local ENU
        const upPointECEF = Cesium.Cartesian3.add(cesiumPositionECEF, cesiumUp, new Cesium.Cartesian3());
        const upLocal = new Cesium.Cartesian3();
        Cesium.Matrix4.multiplyByPoint(this.enuTransformInverse, upPointECEF, upLocal);

        // Update Three.js camera FOV
        this.camera.fov = Cesium.Math.toDegrees(cesiumCamera.frustum.fovy);
        this.camera.updateProjectionMatrix();

        // Set Three.js camera position in local coordinates
        this.camera.position.set(localPosition.x, localPosition.y, localPosition.z);

        // Set camera orientation
        const localDirection = new THREE.Vector3(
            targetLocal.x - localPosition.x,
            targetLocal.y - localPosition.y,
            targetLocal.z - localPosition.z
        );
        const localUpVector = new THREE.Vector3(
            upLocal.x - localPosition.x,
            upLocal.y - localPosition.y,
            upLocal.z - localPosition.z
        );

        this.camera.up.copy(localUpVector.normalize());
        this.camera.lookAt(
            localPosition.x + localDirection.x,
            localPosition.y + localDirection.y,
            localPosition.z + localDirection.z
        );
    }

    setupEventListeners() {
        // Window resize
        window.addEventListener('resize', () => this.onWindowResize());

        const interactionCanvas = this.getInteractionCanvas();

        // Click the Cesium canvas because the Three canvas lets map events pass through.
        interactionCanvas.addEventListener('click', event => this.onMouseClick(event));

        // Mouse move for hover effects
        interactionCanvas.addEventListener('mousemove', event => this.onMouseMove(event));
    }

    getInteractionCanvas() {
        return this.cesiumViewer.scene?.canvas || this.canvas;
    }

    onWindowResize() {
        const width = this.canvas.clientWidth;
        const height = this.canvas.clientHeight;

        this.camera.aspect = width / height;
        this.camera.updateProjectionMatrix();

        this.renderer.setSize(width, height);
    }

    onMouseClick(event) {
        if (performance.now() - this.lastDragEndMs < DRAG_CLICK_SUPPRESS_MS) {
            return;
        }

        // Calculate mouse position in normalized device coordinates
        const rect = this.getInteractionCanvas().getBoundingClientRect();
        this.mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        this.mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

        // Raycast to find intersections
        this.raycaster.setFromCamera(this.mouse, this.camera);

        // Check for track intersections
        if (this.trackRenderer) {
            const trackId = this.trackRenderer.raycast(this.raycaster);
            if (trackId !== null) {
                this.state.selectTrack(trackId);
                if (this.state.settings.autoZoom) {
                    this.state.followTrack(trackId);
                }
            }
        }
    }

    onMouseMove(event) {
        // Update mouse position for hover effects
        const rect = this.getInteractionCanvas().getBoundingClientRect();
        this.mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        this.mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

        // Raycast for hover tooltips
        this.raycaster.setFromCamera(this.mouse, this.camera);

        // Check for track hover
        let hoveredObject = null;
        if (this.trackRenderer) {
            const trackId = this.trackRenderer.raycast(this.raycaster);
            if (trackId !== null) {
                hoveredObject = { trackId };
            }
        }

        // Check for camera hover (if not already hovering track)
        let newHoveredCameraId = null;
        if (!hoveredObject && this.cameraRenderer) {
            const cameraId = this.cameraRenderer.raycast(this.raycaster);
            if (cameraId !== null) {
                hoveredObject = { cameraId };
                newHoveredCameraId = cameraId;
            }
        }

        // Update FOV coverage if hovered camera changed
        if (newHoveredCameraId !== this.hoveredCameraId) {
            this.hoveredCameraId = newHoveredCameraId;
            if (this.fovCoverageRenderer && this.state.visibility.frustums) {
                this.fovCoverageRenderer.update(this.state.cameras, this.state.visibility, this.hoveredCameraId);
            }
        }

        // Update tooltip
        if (this.tooltipManager) {
            this.tooltipManager.update(hoveredObject, event.clientX, event.clientY);
        }
    }

    onStateChange(event, state) {
        // Update renderers based on state changes
        switch (event) {
            case 'tracks':
                if (this.trackRenderer) {
                    this.trackRenderer.update(state.tracks, state.visibility, state.selectedTrackId);
                }
                if (this.rayRenderer && state.visibility.rays !== 'none') {
                    this.rayRenderer.update(state.tracks, state.cameras, state.visibility.rays, state.selectedTrackId);
                }
                this.updateCameraFollow();
                break;

            case 'cameras':
                if (this.cameraRenderer) {
                    const highlightedIds = this.getHighlightedCameraIds(state);
                    this.cameraRenderer.update(state.cameras, state.visibility, highlightedIds);
                }
                if (this.fovCoverageRenderer) {
                    this.fovCoverageRenderer.update(state.cameras, state.visibility, this.hoveredCameraId);
                }
                break;

            case 'weavefield':
                if (this.voxelRenderer) {
                    this.voxelRenderer.update(state.weavefieldHistory, state.visibility);
                }
                break;

            case 'visibility':
                // Update all renderers with new visibility settings
                if (this.cameraRenderer) {
                    const highlightedIds = this.getHighlightedCameraIds(state);
                    this.cameraRenderer.update(state.cameras, state.visibility, highlightedIds);
                }
                if (this.fovCoverageRenderer) {
                    this.fovCoverageRenderer.update(state.cameras, state.visibility, this.hoveredCameraId);
                }
                if (this.trackRenderer) {
                    this.trackRenderer.update(state.tracks, state.visibility, state.selectedTrackId);
                }
                if (this.voxelRenderer) {
                    this.voxelRenderer.update(state.weavefieldHistory, state.visibility);
                }
                if (this.rayRenderer) {
                    this.rayRenderer.update(state.tracks, state.cameras, state.visibility.rays, state.selectedTrackId);
                }
                if (this.gridRenderer) {
                    this.gridRenderer.setVisible(state.visibility.grid);
                }
                break;

            case 'selection':
                if (this.trackRenderer) {
                    this.trackRenderer.update(state.tracks, state.visibility, state.selectedTrackId);
                }
                if (this.cameraRenderer) {
                    const highlightedIds = this.getHighlightedCameraIds(state);
                    this.cameraRenderer.update(state.cameras, state.visibility, highlightedIds);
                }
                if (this.rayRenderer && state.visibility.rays !== 'none') {
                    this.rayRenderer.update(state.tracks, state.cameras, state.visibility.rays, state.selectedTrackId);
                }
                break;

            case 'settings':
                this.applySettings(state.settings);
                break;

            case 'follow':
                this.updateCameraFollow();
                break;
        }
    }

    applySettings(settings, force = false) {
        if (this.gridRenderer) {
            this.gridRenderer.updateScale(settings.scaleValue);
        }
        if (force || settings.scaleValue !== this.lastAppliedScaleValue) {
            this.lastAppliedScaleValue = settings.scaleValue;
            this.applyViewScale(settings.scaleValue);
        }
    }

    applyViewScale(scaleValue) {
        const focus = this.getCurrentFocusPoint();
        if (!focus) {
            return;
        }
        this.lookAtLocalPoint(focus, this.scaleValueToRangeMeters(scaleValue));
    }

    updateCameraFollow() {
        if (!this.state.settings.autoZoom) return;
        const now = performance.now();
        if (now - this.lastAutoFitMs < AUTO_FIT_INTERVAL_MS) {
            return;
        }
        this.lastAutoFitMs = now;

        const followingTrack = this.state.getFollowingTrack();
        if (followingTrack) {
            this.centerOnTrack(followingTrack);
        } else if (this.state.tracks.size > 0) {
            // Auto-zoom to fit all tracks
            this.fitToTracks();
        }
    }

    centerOnTrack(track) {
        const pos = track.state.slice(0, 3);
        this.lookAtLocalPoint(
            new THREE.Vector3(pos[0], pos[1], pos[2]),
            this.scaleValueToRangeMeters(this.state.settings.scaleValue)
        );
    }

    resetCamera() {
        this.cesiumViewer.camera.setView({
            destination: Cesium.Cartesian3.fromDegrees(-122.4194, 37.7749, 2000),
            orientation: {
                heading: Cesium.Math.toRadians(0),
                pitch: Cesium.Math.toRadians(-45),
                roll: 0.0
            }
        });
        this.cesiumViewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    }

    fitToTracks() {
        if (this.state.tracks.size === 0) return;

        const bounds = this.getTrackBounds();
        if (!bounds) return;

        const center = bounds.getCenter(new THREE.Vector3());
        const size = bounds.getSize(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z);

        const distance = Math.max(maxDim * 2, this.scaleValueToRangeMeters(this.state.settings.scaleValue));
        this.flyToLocalPoint(center, distance);
    }

    getTrackBounds() {
        if (this.state.tracks.size === 0) {
            return null;
        }

        const bounds = new THREE.Box3();
        this.state.tracks.forEach(track => {
            const pos = track.state.slice(0, 3);
            bounds.expandByPoint(new THREE.Vector3(pos[0], pos[1], pos[2]));
        });
        return bounds;
    }

    getCurrentFocusPoint() {
        const followingTrack = this.state.getFollowingTrack();
        const selectedTrack = this.state.getActiveTrack();
        const track = followingTrack || selectedTrack;
        if (track) {
            return new THREE.Vector3(track.state[0], track.state[1], track.state[2]);
        }

        const bounds = this.getTrackBounds();
        if (bounds) {
            return bounds.getCenter(new THREE.Vector3());
        }

        return new THREE.Vector3(0, 0, 0);
    }

    scaleValueToRangeMeters(scaleValue) {
        const t = Math.min(100, Math.max(0, scaleValue)) / 100;
        return SCALE_MIN_RANGE_M * Math.pow(SCALE_MAX_RANGE_M / SCALE_MIN_RANGE_M, t);
    }

    lookAtLocalPoint(localPoint, rangeMeters) {
        const ecefPoint = this.localToEcef(localPoint);
        const camera = this.cesiumViewer.camera;
        const heading = Number.isFinite(camera.heading) ? camera.heading : 0;
        const pitch = Number.isFinite(camera.pitch) ? camera.pitch : DEFAULT_VIEW_PITCH_RAD;

        camera.lookAt(
            ecefPoint,
            new Cesium.HeadingPitchRange(heading, pitch, rangeMeters)
        );
        camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    }

    flyToLocalPoint(localPoint, rangeMeters) {
        const ecefCenter = this.localToEcef(localPoint);
        const cartographic = Cesium.Cartographic.fromCartesian(ecefCenter);

        this.cesiumViewer.camera.flyTo({
            destination: Cesium.Cartesian3.fromRadians(
                cartographic.longitude,
                cartographic.latitude,
                cartographic.height + rangeMeters
            ),
            orientation: {
                heading: Cesium.Math.toRadians(0),
                pitch: DEFAULT_VIEW_PITCH_RAD,
                roll: 0.0
            },
            duration: 2.0
        });
    }

    localToEcef(localPoint) {
        const localCartesian = new Cesium.Cartesian3(localPoint.x, localPoint.y, localPoint.z);
        const ecefPoint = new Cesium.Cartesian3();
        Cesium.Matrix4.multiplyByPoint(this.enuTransform, localCartesian, ecefPoint);
        return ecefPoint;
    }

    getHighlightedCameraIds(state) {
        // Get camera IDs to highlight based on selected track
        if (state.selectedTrackId === null) {
            return [];
        }
        const selectedTrack = state.tracks.get(state.selectedTrackId);
        if (!selectedTrack) {
            return [];
        }
        return selectedTrack.visible_camera_ids || [];
    }

    render() {
        // Sync Three.js camera with Cesium before rendering
        this.syncCameraWithCesium();

        // Render Three.js overlay directly to preserve canvas transparency.
        this.renderer.render(this.scene, this.camera);
    }
}
