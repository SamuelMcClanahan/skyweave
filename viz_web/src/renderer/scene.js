// Scene manager: Cesium earth context plus a transparent Three.js overlay.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

import { CameraRenderer } from './cameras.js';
import { TrackRenderer } from './tracks.js';
import { VoxelRenderer } from './voxels.js';
import { RayRenderer } from './rays.js';
import { GridRenderer } from './grid.js';
import { FovCoverageRenderer } from './fov-coverage.js';
import { TooltipManager } from '../ui/tooltip-manager.js';

const OSM_TILE_URL = 'https://tile.openstreetmap.org/';
const OSM_CREDIT = 'Map data (c) OpenStreetMap contributors';
const EARTH_FALLBACK_COLOR = '#182331';
const AUTO_FIT_INTERVAL_MS = 1500;
const SCALE_MIN_RANGE_M = 50;
const SCALE_MAX_RANGE_M = 10000;
const ROOM_SCALE_MIN_RANGE_M = 3;
const ROOM_SCALE_MAX_RANGE_M = 16;
const AIRSPACE_SCALE_MIN_RANGE_M = 20;
const AIRSPACE_SCALE_MAX_RANGE_M = 160;
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
        this.roomGroup = null;
        this.roomMesh = null;
        this.roomFallback = null;
        this.roomLoader = new GLTFLoader();
        this.loadedRoomUrl = '';
        this.useCesium = false;
        this.sceneMode = state.settings.sceneMode;

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
        window.skyweaveVizStatus?.('initializing Cesium viewer');
        if (this.sceneMode === 'map') {
            this.initCesium();
        } else {
            this.disableCesiumLayer();
        }

        // Initialize Three.js as the transparent tactical overlay.
        window.skyweaveVizStatus?.('initializing Three overlay');
        this.initThree();

        // Initialize renderers
        this.cameraRenderer = new CameraRenderer(this.scene);
        this.trackRenderer = new TrackRenderer(this.scene);
        this.voxelRenderer = new VoxelRenderer(this.scene);
        this.rayRenderer = new RayRenderer(this.scene);
        this.gridRenderer = new GridRenderer(this.scene);
        this.fovCoverageRenderer = new FovCoverageRenderer(this.scene);
        this.roomGroup = new THREE.Group();
        this.roomGroup.name = 'Room Scan';
        this.scene.add(this.roomGroup);
        this.tooltipManager = new TooltipManager(this.state);
        window.skyweaveVizStatus?.('applying visualizer settings');
        this.applySettings(this.state.settings, true);
        this.applyRoomSettings(this.state.room, true);

        // Setup event listeners
        window.skyweaveVizStatus?.('installing visualizer input handlers');
        this.setupEventListeners();

        // Subscribe to state changes
        this.state.subscribe(this.onStateChange.bind(this));
    }

    initCesium() {
        if (typeof Cesium === 'undefined') {
            this.useCesium = false;
            const container = document.getElementById('cesiumContainer');
            if (container) {
                container.style.display = 'none';
            }
            console.warn('Cesium unavailable; using local Three.js fallback scene.');
            return;
        }
        this.useCesium = true;
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
            alpha: this.useCesium,
            antialias: true
        });
        this.renderer.setSize(width, height);
        this.renderer.setPixelRatio(window.devicePixelRatio);
        this.renderer.setClearColor(0x05070a, this.useCesium ? 0 : 1);
        this.renderer.autoClear = true;
        if (!this.useCesium) {
            this.camera.position.set(6, -9, 5);
            this.camera.up.set(0, 0, 1);
            this.camera.lookAt(0, 0, 1);
        }

        this.controls = new OrbitControls(this.camera, this.canvas);
        this.controls.enableDamping = true;
        this.controls.dampingFactor = 0.08;
        this.controls.screenSpacePanning = true;
        this.controls.target.set(0, 0, 1);
        this.controls.enabled = !this.useCesium;

        // Lighting
        const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
        this.scene.add(ambientLight);

        const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
        directionalLight.position.set(50, 50, 50);
        this.scene.add(directionalLight);
    }

    disableCesiumLayer() {
        this.useCesium = false;
        const container = document.getElementById('cesiumContainer');
        if (container) {
            container.style.display = 'none';
        }
    }

    setSceneMode(sceneMode) {
        const nextMode = sceneMode === 'map' || sceneMode === 'airspace' ? sceneMode : 'room';
        this.sceneMode = nextMode;
        const container = document.getElementById('cesiumContainer');
        if (nextMode === 'map') {
            if (!this.cesiumViewer && typeof Cesium !== 'undefined') {
                this.initCesium();
            }
            if (container) container.style.display = 'block';
            this.useCesium = Boolean(this.cesiumViewer);
        } else {
            this.disableCesiumLayer();
        }
        if (this.controls) {
            this.controls.enabled = !this.useCesium;
        }
        if (this.canvas) {
            this.canvas.style.pointerEvents = this.useCesium ? 'none' : 'auto';
        }
        if (this.renderer) {
            this.renderer.setClearColor(0x05070a, this.useCesium ? 0 : 1);
        }
        this.onWindowResize();
        this.applyViewScale(this.state.settings.scaleValue);
    }

    syncCameraWithCesium() {
        if (!this.useCesium || !this.cesiumViewer) {
            return;
        }
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
        return this.cesiumViewer?.scene?.canvas || this.canvas;
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
            this.fovCoverageRenderer.update(this.state.cameras, this.state.visibility, this.hoveredCameraId, this.state.settings);
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
                    this.trackRenderer.update(state.tracks, state.visibility, state.selectedTrackId, state.settings);
                }
                if (this.rayRenderer && state.visibility.rays !== 'none') {
                    this.rayRenderer.update(state.tracks, state.cameras, state.visibility.rays, state.selectedTrackId);
                }
                this.updateOriginTrackView();
                this.updateCameraFollow();
                break;

            case 'cameras':
                if (this.cameraRenderer) {
                    const highlightedIds = this.getHighlightedCameraIds(state);
                    this.cameraRenderer.update(state.cameras, state.visibility, highlightedIds, state.settings);
                }
                if (this.fovCoverageRenderer) {
                    this.fovCoverageRenderer.update(state.cameras, state.visibility, this.hoveredCameraId, state.settings);
                }
                break;

            case 'weavefield':
                if (this.voxelRenderer) {
                    this.voxelRenderer.update(state.weavefieldHistory, state.visibility, state.settings);
                }
                break;

            case 'visibility':
                // Update all renderers with new visibility settings
                if (this.cameraRenderer) {
                    const highlightedIds = this.getHighlightedCameraIds(state);
                    this.cameraRenderer.update(state.cameras, state.visibility, highlightedIds, state.settings);
                }
                if (this.fovCoverageRenderer) {
                    this.fovCoverageRenderer.update(state.cameras, state.visibility, this.hoveredCameraId, state.settings);
                }
                if (this.trackRenderer) {
                    this.trackRenderer.update(state.tracks, state.visibility, state.selectedTrackId, state.settings);
                }
                if (this.voxelRenderer) {
                    this.voxelRenderer.update(state.weavefieldHistory, state.visibility, state.settings);
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
                    this.trackRenderer.update(state.tracks, state.visibility, state.selectedTrackId, state.settings);
                }
                if (this.cameraRenderer) {
                    const highlightedIds = this.getHighlightedCameraIds(state);
                    this.cameraRenderer.update(state.cameras, state.visibility, highlightedIds, state.settings);
                }
                if (this.rayRenderer && state.visibility.rays !== 'none') {
                    this.rayRenderer.update(state.tracks, state.cameras, state.visibility.rays, state.selectedTrackId);
                }
                break;

            case 'settings':
                this.applySettings(state.settings);
                break;

            case 'room':
                this.applyRoomSettings(state.room);
                break;

            case 'follow':
                this.updateCameraFollow();
                break;
        }
    }

    applySettings(settings, force = false) {
        this.setSceneMode(settings.sceneMode);
        if (this.gridRenderer) {
            this.gridRenderer.updateScale(settings.scaleValue, settings.sceneMode);
        }
        const highlightedIds = this.getHighlightedCameraIds(this.state);
        this.cameraRenderer?.update(this.state.cameras, this.state.visibility, highlightedIds, settings);
        this.fovCoverageRenderer?.update(this.state.cameras, this.state.visibility, this.hoveredCameraId, settings);
        this.trackRenderer?.update(this.state.tracks, this.state.visibility, this.state.selectedTrackId, settings);
        this.voxelRenderer?.update(this.state.weavefieldHistory, this.state.visibility, settings);
        if (force || settings.scaleValue !== this.lastAppliedScaleValue) {
            this.lastAppliedScaleValue = settings.scaleValue;
            this.applyViewScale(settings.scaleValue);
        }
        this.updateOriginTrackView();
    }

    applyRoomSettings(room, force = false) {
        if (!this.roomGroup) {
            return;
        }

        const meshUrl = room?.mesh_url || '';
        if (force || meshUrl !== this.loadedRoomUrl) {
            this.loadRoomMesh(meshUrl);
        }
        this.roomGroup.visible = Boolean(room?.visible || room?.fallback_visible);
        this.roomGroup.position.set(...(room?.translation_m || [0, 0, 0]));
        const rotation = room?.rotation_deg || [0, 0, 0];
        this.roomGroup.rotation.set(
            THREE.MathUtils.degToRad(rotation[0] || 0),
            THREE.MathUtils.degToRad(rotation[1] || 0),
            THREE.MathUtils.degToRad(rotation[2] || 0)
        );
        const scale = Number(room?.scale || 1);
        this.roomGroup.scale.setScalar(scale);
        if (this.roomMesh) {
            this.roomMesh.visible = Boolean(room?.visible && meshUrl);
            this.fitRoomMeshToFallback(this.roomMesh, room);
            this.applyRoomOpacity(this.roomMesh, Number(room?.opacity ?? 0.42));
        }
        this.updateFallbackRoom(room);
    }

    loadRoomMesh(meshUrl) {
        this.loadedRoomUrl = meshUrl || '';
        if (this.roomMesh) {
            this.roomGroup.remove(this.roomMesh);
            this.disposeObject(this.roomMesh);
            this.roomMesh = null;
        }
        if (!meshUrl) {
            return;
        }
        this.roomLoader.load(
            meshUrl,
            gltf => {
                if (meshUrl !== this.loadedRoomUrl) {
                    return;
                }
                this.roomMesh = new THREE.Group();
                this.roomMesh.name = 'Room Mesh';
                gltf.scene.name = 'Room Mesh Content';
                this.roomMesh.userData.content = gltf.scene;
                this.roomMesh.add(gltf.scene);
                this.fitRoomMeshToFallback(this.roomMesh, this.state.room);
                this.applyRoomOpacity(this.roomMesh, Number(this.state.room?.opacity ?? 0.42));
                this.roomGroup.add(this.roomMesh);
                this.applyRoomSettings(this.state.room, false);
            },
            undefined,
            error => {
                console.warn('Room mesh failed to load', meshUrl, error);
            }
        );
    }

    fitRoomMeshToFallback(wrapper, room) {
        const content = wrapper?.userData?.content;
        if (!content) {
            return;
        }

        const fallbackSize = room?.fallback_size_m || [4, 4, 2.6];
        const fitKey = fallbackSize.join(',');
        if (wrapper.userData.fitKey === fitKey) {
            return;
        }

        content.position.set(0, 0, 0);
        content.scale.setScalar(1);
        content.updateMatrixWorld(true);

        const box = new THREE.Box3().setFromObject(content);
        if (box.isEmpty()) {
            return;
        }

        const meshSize = box.getSize(new THREE.Vector3());
        const targetSize = new THREE.Vector3(
            Math.max(Number(fallbackSize[0]) || 4, 0.001),
            Math.max(Number(fallbackSize[1]) || 4, 0.001),
            Math.max(Number(fallbackSize[2]) || 2.6, 0.001)
        );
        const safeMeshSize = new THREE.Vector3(
            Math.max(meshSize.x, 0.001),
            Math.max(meshSize.y, 0.001),
            Math.max(meshSize.z, 0.001)
        );
        const fitScale = 0.92 * Math.min(
            targetSize.x / safeMeshSize.x,
            targetSize.y / safeMeshSize.y,
            targetSize.z / safeMeshSize.z
        );
        const center = box.getCenter(new THREE.Vector3());
        const targetCenter = new THREE.Vector3(0, 0, targetSize.z / 2);

        content.scale.setScalar(fitScale);
        content.position.copy(targetCenter).sub(center.multiplyScalar(fitScale));
        wrapper.userData.fitKey = fitKey;
        wrapper.userData.fitScale = fitScale;
    }

    updateFallbackRoom(room) {
        const size = room?.fallback_size_m || [4, 4, 2.6];
        const key = size.join(',');
        if (!this.roomFallback || this.roomFallback.userData.sizeKey !== key) {
            if (this.roomFallback) {
                this.roomGroup.remove(this.roomFallback);
                this.disposeObject(this.roomFallback);
            }
            const geometry = new THREE.BoxGeometry(size[0], size[1], size[2]);
            const edges = new THREE.EdgesGeometry(geometry);
            const material = new THREE.LineBasicMaterial({ color: 0x79c6ff, transparent: true, opacity: 0.32 });
            this.roomFallback = new THREE.LineSegments(edges, material);
            this.roomFallback.name = 'Measured Room Fallback';
            this.roomFallback.position.z = size[2] / 2;
            this.roomFallback.userData.sizeKey = key;
            this.roomGroup.add(this.roomFallback);
        }
        this.roomFallback.visible = Boolean(room?.fallback_visible);
    }

    applyRoomOpacity(object, opacity) {
        object.traverse(child => {
            if (!child.material) {
                return;
            }
            const materials = Array.isArray(child.material) ? child.material : [child.material];
            materials.forEach(material => {
                material.transparent = opacity < 1.0;
                material.opacity = opacity;
                material.depthWrite = opacity >= 0.95;
                material.needsUpdate = true;
            });
        });
    }

    disposeObject(object) {
        object.traverse(child => {
            child.geometry?.dispose?.();
            const materials = child.material ? (Array.isArray(child.material) ? child.material : [child.material]) : [];
            materials.forEach(material => material.dispose?.());
        });
    }

    applyViewScale(scaleValue) {
        if (this.state.settings.originTrackView) {
            return;
        }
        const focus = this.getCurrentFocusPoint();
        if (!focus) {
            return;
        }
        this.lookAtLocalPoint(focus, this.scaleValueToRangeMeters(scaleValue));
    }

    updateCameraFollow() {
        if (this.state.settings.originTrackView) return;
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
        if (!this.useCesium || !this.cesiumViewer) {
            if (this.state.settings.sceneMode === 'airspace') {
                this.camera.position.set(38, -54, 34);
                this.camera.up.set(0, 0, 1);
                this.camera.lookAt(0, 0, 8);
                this.controls?.target.set(0, 0, 8);
                this.controls?.update();
                return;
            }
            this.camera.position.set(6, -9, 5);
            this.camera.up.set(0, 0, 1);
            this.camera.lookAt(0, 0, 1);
            this.controls?.target.set(0, 0, 1);
            this.controls?.update();
            return;
        }
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
        if (this.state.settings.sceneMode === 'room') {
            return ROOM_SCALE_MIN_RANGE_M * Math.pow(ROOM_SCALE_MAX_RANGE_M / ROOM_SCALE_MIN_RANGE_M, t);
        }
        if (this.state.settings.sceneMode === 'airspace') {
            return AIRSPACE_SCALE_MIN_RANGE_M * Math.pow(AIRSPACE_SCALE_MAX_RANGE_M / AIRSPACE_SCALE_MIN_RANGE_M, t);
        }
        return SCALE_MIN_RANGE_M * Math.pow(SCALE_MAX_RANGE_M / SCALE_MIN_RANGE_M, t);
    }

    lookAtLocalPoint(localPoint, rangeMeters) {
        if (!this.useCesium || !this.cesiumViewer) {
            const sceneMode = this.state.settings.sceneMode;
            const maxRange = sceneMode === 'room' ? 24 : sceneMode === 'airspace' ? 220 : 10000;
            const minRange = sceneMode === 'room' ? 2.5 : 8;
            const range = Math.max(minRange, Math.min(Number(rangeMeters) || 8, maxRange));
            this.camera.position.set(localPoint.x + range * 0.7, localPoint.y - range, localPoint.z + range * 0.55);
            this.camera.up.set(0, 0, 1);
            this.camera.lookAt(localPoint.x, localPoint.y, localPoint.z);
            this.controls?.target.set(localPoint.x, localPoint.y, localPoint.z);
            this.controls?.update();
            return;
        }
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
        if (!this.useCesium || !this.cesiumViewer) {
            this.lookAtLocalPoint(localPoint, rangeMeters);
            return;
        }
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

    updateOriginTrackView() {
        if (!this.state.settings.originTrackView || this.useCesium) {
            return;
        }
        const track = this.state.getActiveTrack() || this.state.getFollowingTrack() || this.firstTrack();
        if (!track || !Array.isArray(track.state) || track.state.length < 3) {
            return;
        }
        const target = new THREE.Vector3(track.state[0], track.state[1], track.state[2]);
        if (target.length() < 0.1) {
            return;
        }
        this.camera.position.set(0, 0, 0);
        this.camera.up.set(0, 0, 1);
        this.camera.lookAt(target);
        this.controls?.target.copy(target);
        this.controls?.update();
    }

    firstTrack() {
        for (const track of this.state.tracks.values()) {
            return track;
        }
        return null;
    }

    localToEcef(localPoint) {
        if (!this.useCesium || !this.enuTransform) {
            return localPoint;
        }
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
        if (this.useCesium) {
            this.syncCameraWithCesium();
        } else if (this.controls) {
            this.controls.update();
        }

        // Render Three.js overlay directly to preserve canvas transparency.
        this.renderer.render(this.scene, this.camera);
    }
}
