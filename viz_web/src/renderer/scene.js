// Scene manager - handles Three.js initialization and rendering
import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';

import { CameraRenderer } from './cameras.js';
import { TrackRenderer } from './tracks.js';
import { VoxelRenderer } from './voxels.js';
import { RayRenderer } from './rays.js';
import { GridRenderer } from './grid.js';

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
        this.composer = null;

        // Renderers for different object types
        this.cameraRenderer = null;
        this.trackRenderer = null;
        this.voxelRenderer = null;
        this.rayRenderer = null;
        this.gridRenderer = null;

        // Raycaster for mouse interaction
        this.raycaster = new THREE.Raycaster();
        this.mouse = new THREE.Vector2();

        this.canvas = null;
    }

    async init() {
        this.canvas = document.getElementById('three-canvas');

        // Initialize Cesium first (底层)
        this.initCesium();

        // Initialize Three.js overlay (覆盖层)
        this.initThree();

        // Initialize renderers
        this.cameraRenderer = new CameraRenderer(this.scene);
        this.trackRenderer = new TrackRenderer(this.scene);
        this.voxelRenderer = new VoxelRenderer(this.scene);
        this.rayRenderer = new RayRenderer(this.scene);
        this.gridRenderer = new GridRenderer(this.scene);

        // Setup event listeners
        this.setupEventListeners();

        // Subscribe to state changes
        this.state.subscribe(this.onStateChange.bind(this));
    }

    initCesium() {
        // Initialize Cesium viewer focused on San Francisco
        this.cesiumViewer = new Cesium.Viewer('cesiumContainer', {
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
            shadows: false,
            terrainProvider: Cesium.createWorldTerrain(),
            imageryProvider: new Cesium.IonImageryProvider({ assetId: 2 })
        });

        // Define local origin for our tracking coordinate system (SF downtown)
        // This is the reference point: our local (0,0,0) corresponds to this lat/lon
        this.localOriginLongitude = -122.4194;
        this.localOriginLatitude = 37.7749;
        this.localOriginCartesian = Cesium.Cartesian3.fromDegrees(
            this.localOriginLongitude,
            this.localOriginLatitude,
            0
        );

        // Compute the ENU (East-North-Up) transformation matrix at our local origin
        // This converts from ECEF to our local coordinate system
        this.enuTransform = Cesium.Transforms.eastNorthUpToFixedFrame(this.localOriginCartesian);
        this.enuTransformInverse = Cesium.Matrix4.inverse(this.enuTransform, new Cesium.Matrix4());

        // Set initial view to San Francisco downtown
        this.cesiumViewer.camera.setView({
            destination: Cesium.Cartesian3.fromDegrees(-122.4194, 37.7749, 2000), // SF coords + 2km altitude
            orientation: {
                heading: Cesium.Math.toRadians(0),
                pitch: Cesium.Math.toRadians(-45),
                roll: 0.0
            }
        });

        // Disable Cesium's default atmosphere effects for cleaner look
        this.cesiumViewer.scene.skyAtmosphere.show = false;
        this.cesiumViewer.scene.sun.show = false;
        this.cesiumViewer.scene.moon.show = false;
        this.cesiumViewer.scene.backgroundColor = Cesium.Color.BLACK;
    }

    initThree() {
        // Create scene with transparent background to overlay on Cesium
        this.scene = new THREE.Scene();
        // No background color - fully transparent to show Cesium underneath

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

        // No OrbitControls - Cesium handles camera navigation
        // Three.js camera will sync to Cesium's camera position

        // Setup post-processing (bloom effect)
        this.composer = new EffectComposer(this.renderer);

        const renderPass = new RenderPass(this.scene, this.camera);
        this.composer.addPass(renderPass);

        const bloomPass = new UnrealBloomPass(
            new THREE.Vector2(width, height),
            0.3,  // strength - much more subtle
            0.3,  // radius
            0.9   // threshold - higher = less glow
        );
        this.composer.addPass(bloomPass);

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

        // Mouse click for track selection
        this.canvas.addEventListener('click', (event) => this.onMouseClick(event));

        // Mouse move for hover effects
        this.canvas.addEventListener('mousemove', (event) => this.onMouseMove(event));
    }

    onWindowResize() {
        const width = this.canvas.clientWidth;
        const height = this.canvas.clientHeight;

        this.camera.aspect = width / height;
        this.camera.updateProjectionMatrix();

        this.renderer.setSize(width, height);
        this.composer.setSize(width, height);
    }

    onMouseClick(event) {
        // Calculate mouse position in normalized device coordinates
        const rect = this.canvas.getBoundingClientRect();
        this.mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        this.mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

        // Raycast to find intersections
        this.raycaster.setFromCamera(this.mouse, this.camera);

        // Check for track intersections
        if (this.trackRenderer) {
            const trackId = this.trackRenderer.raycast(this.raycaster);
            if (trackId !== null) {
                this.state.selectTrack(trackId);
            }
        }
    }

    onMouseMove(event) {
        // Update mouse position for hover effects
        const rect = this.canvas.getBoundingClientRect();
        this.mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        this.mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    }

    onStateChange(event, state) {
        // Update renderers based on state changes
        switch (event) {
            case 'tracks':
                if (this.trackRenderer) {
                    this.trackRenderer.update(state.tracks, state.visibility, state.selectedTrackId);
                }
                if (this.rayRenderer && state.visibility.rays !== 'none') {
                    this.rayRenderer.update(state.tracks, state.cameras, state.visibility.rays);
                }
                this.updateCameraFollow();
                break;

            case 'cameras':
                if (this.cameraRenderer) {
                    this.cameraRenderer.update(state.cameras, state.visibility);
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
                    this.cameraRenderer.update(state.cameras, state.visibility);
                }
                if (this.trackRenderer) {
                    this.trackRenderer.update(state.tracks, state.visibility, state.selectedTrackId);
                }
                if (this.voxelRenderer) {
                    this.voxelRenderer.update(state.weavefieldHistory, state.visibility);
                }
                if (this.rayRenderer) {
                    this.rayRenderer.update(state.tracks, state.cameras, state.visibility.rays);
                }
                if (this.gridRenderer) {
                    this.gridRenderer.setVisible(state.visibility.grid);
                }
                break;

            case 'follow':
                this.updateCameraFollow();
                break;
        }
    }

    updateCameraFollow() {
        if (!this.state.settings.autoZoom) return;

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
        const localPoint = new Cesium.Cartesian3(pos[0], pos[1], pos[2]);
        const ecefPoint = new Cesium.Cartesian3();
        Cesium.Matrix4.multiplyByPoint(this.enuTransform, localPoint, ecefPoint);
        this.cesiumViewer.camera.lookAt(
            ecefPoint,
            new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-45), 500)
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

        const bounds = new THREE.Box3();
        this.state.tracks.forEach(track => {
            const pos = track.state.slice(0, 3);
            bounds.expandByPoint(new THREE.Vector3(pos[0], pos[1], pos[2]));
        });

        const center = bounds.getCenter(new THREE.Vector3());
        const size = bounds.getSize(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z);

        // Determine distance based on track classification if available
        let distance = maxDim * 2;
        if (this.state.tracks.size === 1) {
            const track = Array.from(this.state.tracks.values())[0];
            if (track.classification === 'plane') {
                distance = 2000;
            } else if (track.classification === 'drone') {
                distance = 500;
            } else {
                distance = 300;
            }
        }

        // Convert local ENU center to ECEF for Cesium
        const localCenter = new Cesium.Cartesian3(center.x, center.y, center.z);
        const ecefCenter = new Cesium.Cartesian3();
        Cesium.Matrix4.multiplyByPoint(this.enuTransform, localCenter, ecefCenter);

        // Convert to Cartographic to get lat/lon/height
        const cartographic = Cesium.Cartographic.fromCartesian(ecefCenter);

        // Use Cesium camera to fly to the view
        this.cesiumViewer.camera.flyTo({
            destination: Cesium.Cartesian3.fromRadians(
                cartographic.longitude,
                cartographic.latitude,
                cartographic.height + distance
            ),
            orientation: {
                heading: Cesium.Math.toRadians(0),
                pitch: Cesium.Math.toRadians(-45),
                roll: 0.0
            },
            duration: 2.0
        });
    }

    render() {
        // Sync Three.js camera with Cesium before rendering
        this.syncCameraWithCesium();

        // Render Three.js overlay with bloom effect
        this.composer.render();
    }
}
