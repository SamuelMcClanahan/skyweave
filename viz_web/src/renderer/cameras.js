// Camera node and frustum rendering
import * as THREE from 'three';

export class CameraRenderer {
    constructor(scene) {
        this.scene = scene;
        this.cameraGroup = new THREE.Group();
        this.cameraGroup.name = 'cameras';
        this.scene.add(this.cameraGroup);

        this.cameraMeshes = new Map();
        this.frustumMeshes = new Map();
    }

    update(cameras, visibility) {
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

        // Camera body (small pyramid/cone)
        const cameraGeometry = new THREE.ConeGeometry(0.3, 0.6, 4);
        const cameraMaterial = new THREE.MeshStandardMaterial({
            color: 0xffffff,
            emissive: 0xffffff,
            emissiveIntensity: 0.2,
            metalness: 0.3,
            roughness: 0.5
        });

        const cameraMesh = new THREE.Mesh(cameraGeometry, cameraMaterial);
        cameraMesh.rotation.x = Math.PI / 2; // Point forward
        group.add(cameraMesh);

        // Position the camera node
        const pos = camera.position;
        group.position.set(pos[0], pos[1], pos[2]);

        // Create frustum wireframe if enabled
        if (showFrustum) {
            const frustum = this.createFrustum(camera);
            group.add(frustum);
            this.frustumMeshes.set(camera.id, frustum);
        }

        // Add label (using CSS2DRenderer would be better, but using sprite for now)
        const label = this.createLabel(`CAM ${camera.id}`);
        label.position.set(0, 0, 1);
        group.add(label);

        this.cameraMeshes.set(camera.id, cameraMesh);
        this.cameraGroup.add(group);
    }

    createFrustum(camera) {
        // Create wireframe frustum showing camera FOV
        const fovH = camera.fov_h_deg * (Math.PI / 180);
        const fovV = camera.fov_v_deg * (Math.PI / 180);
        const depth = 10; // Frustum depth in meters

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

        const material = new THREE.LineBasicMaterial({
            color: 0xffffff,
            transparent: true,
            opacity: 0.2,
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
        sprite.scale.set(2, 0.5, 1);

        return sprite;
    }
}
