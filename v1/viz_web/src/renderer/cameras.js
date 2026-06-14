// Camera node and frustum rendering
import * as THREE from 'three';

const CAMERA_BODY_RADIUS_M = 14;
const CAMERA_BODY_LENGTH_M = 28;
const CAMERA_FRUSTUM_DEPTH_M = 650;
const CAMERA_LABEL_OFFSET_M = 34;
const CAMERA_LABEL_SCALE = [110, 28, 1];

export class CameraRenderer {
    constructor(scene) {
        this.scene = scene;
        this.cameraGroup = new THREE.Group();
        this.cameraGroup.name = 'cameras';
        this.scene.add(this.cameraGroup);

        this.cameraMeshes = new Map();
        this.frustumMeshes = new Map();
        this.highlightedCameraIds = new Set();
        this.glyphScale = 1.0;
        this.frustumRangeM = CAMERA_FRUSTUM_DEPTH_M;
    }

    update(cameras, visibility, highlightedCameraIds = [], settings = {}) {
        this.highlightedCameraIds = new Set(highlightedCameraIds);
        this.glyphScale = Number(settings.glyphScale || 1.0);
        this.frustumRangeM = Number(settings.frustumRangeM || CAMERA_FRUSTUM_DEPTH_M);

        // Clear existing meshes
        this.cameraGroup.children.forEach(child => {
            if (child.geometry) child.geometry.dispose();
            if (child.material) child.material.dispose();
        });
        this.cameraGroup.clear();
        this.cameraMeshes.clear();
        this.frustumMeshes.clear();

        if (!visibility.cameras) return;

        // Create meshes for each camera
        cameras.forEach(camera => {
            this.createCameraNode(camera, visibility.frustums);
        });
    }

    createCameraNode(camera, showFrustum) {
        const group = new THREE.Group();

        // Check if this camera should be highlighted
        const isHighlighted = this.highlightedCameraIds.has(camera.id);

        // Camera body. This is a visible glyph, not the physical camera size.
        const scale = this.glyphScale;
        const cameraGeometry = new THREE.ConeGeometry(CAMERA_BODY_RADIUS_M * scale, CAMERA_BODY_LENGTH_M * scale, 4);
        const cameraColor = isHighlighted ? 0x00e5ff : 0xffffff;
        const cameraMaterial = new THREE.MeshStandardMaterial({
            color: cameraColor,
            emissive: cameraColor,
            emissiveIntensity: isHighlighted ? 0.6 : 0.2,
            metalness: 0.3,
            roughness: 0.5
        });

        const cameraMesh = new THREE.Mesh(cameraGeometry, cameraMaterial);
        cameraMesh.rotation.x = Math.PI / 2; // Point forward
        group.add(cameraMesh);

        // Position the camera node
        const pos = camera.position;
        group.position.set(pos[0], pos[1], pos[2]);
        this.applyCameraRotation(group, camera.rotation_quat);

        // Create frustum wireframe if enabled
        if (showFrustum) {
            const frustum = this.createFrustum(camera, isHighlighted);
            group.add(frustum);
            this.frustumMeshes.set(camera.id, frustum);
        }

        // Add label (using CSS2DRenderer would be better, but using sprite for now)
        const label = this.createLabel(`CAM ${camera.id}`);
        label.position.set(0, 0, CAMERA_LABEL_OFFSET_M * scale);
        group.add(label);

        this.cameraMeshes.set(camera.id, cameraMesh);
        this.cameraGroup.add(group);
    }

    applyCameraRotation(group, rotationQuat) {
        if (!Array.isArray(rotationQuat) || rotationQuat.length !== 4) {
            return;
        }

        group.quaternion
            .set(rotationQuat[0], rotationQuat[1], rotationQuat[2], rotationQuat[3])
            .normalize();
    }

    createFrustum(camera, isHighlighted = false) {
        // Create wireframe frustum showing camera FOV
        const fovH = camera.fov_h_deg * (Math.PI / 180);
        const fovV = camera.fov_v_deg * (Math.PI / 180);
        const depth = this.frustumRangeM;

        const halfWidth = Math.tan(fovH / 2) * depth;
        const halfHeight = Math.tan(fovV / 2) * depth;

        const geometry = new THREE.BufferGeometry();
        const vertices = new Float32Array([
            // Near plane (at origin)
            0, 0, 0,
            // Far plane corners
            -halfWidth, -halfHeight, depth,
            halfWidth, -halfHeight, depth,
            halfWidth, halfHeight, depth,
            -halfWidth, halfHeight, depth,
            // Lines from origin to corners
            0, 0, 0, -halfWidth, -halfHeight, depth,
            0, 0, 0, halfWidth, -halfHeight, depth,
            0, 0, 0, halfWidth, halfHeight, depth,
            0, 0, 0, -halfWidth, halfHeight, depth,
            // Far plane rectangle
            -halfWidth, -halfHeight, depth, halfWidth, -halfHeight, depth,
            halfWidth, -halfHeight, depth, halfWidth, halfHeight, depth,
            halfWidth, halfHeight, depth, -halfWidth, halfHeight, depth,
            -halfWidth, halfHeight, depth, -halfWidth, -halfHeight, depth,
        ]);

        geometry.setAttribute('position', new THREE.BufferAttribute(vertices, 3));

        const color = isHighlighted ? 0x00e5ff : 0xffffff;
        const opacity = isHighlighted ? 0.4 : 0.35;

        const material = new THREE.LineBasicMaterial({
            color: color,
            transparent: true,
            opacity: opacity,
            linewidth: 1
        });

        return new THREE.LineSegments(geometry, material);
    }

    createLabel(text) {
        // Create a sprite-based label
        const canvas = document.createElement('canvas');
        const context = canvas.getContext('2d');
        canvas.width = 256;
        canvas.height = 64;

        context.fillStyle = 'rgba(0, 0, 0, 0)';
        context.fillRect(0, 0, canvas.width, canvas.height);

        context.font = '32px Inter, sans-serif';
        context.fillStyle = '#ffffff';
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(text, canvas.width / 2, canvas.height / 2);

        const texture = new THREE.CanvasTexture(canvas);
        const material = new THREE.SpriteMaterial({
            map: texture,
            transparent: true
        });

        const sprite = new THREE.Sprite(material);
        sprite.scale.set(
            CAMERA_LABEL_SCALE[0] * this.glyphScale,
            CAMERA_LABEL_SCALE[1] * this.glyphScale,
            CAMERA_LABEL_SCALE[2]
        );

        return sprite;
    }

    raycast(raycaster) {
        const intersects = raycaster.intersectObjects(Array.from(this.cameraMeshes.values()));
        if (intersects.length > 0) {
            // Find the camera ID from the mesh
            for (const [cameraId, mesh] of this.cameraMeshes.entries()) {
                if (mesh === intersects[0].object) {
                    return cameraId;
                }
            }
        }
        return null;
    }
}
