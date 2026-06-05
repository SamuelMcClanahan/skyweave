// FOV Coverage renderer - shaded frustum volumes with overlap darkening
import * as THREE from 'three';

const COVERAGE_FRUSTUM_DEPTH_M = 650.0; // Default size
const COVERAGE_FRUSTUM_DEPTH_HOVER_M = 7200.0; // Full size on hover
const COVERAGE_BASE_OPACITY = 0.08; // Very light base
const COVERAGE_HOVER_OPACITY = 0.15; // Brighter on hover

export class FovCoverageRenderer {
    constructor(scene) {
        this.scene = scene;
        this.coverageGroup = new THREE.Group();
        this.coverageGroup.name = 'fov-coverage';
        this.scene.add(this.coverageGroup);

        this.frustumMeshes = new Map();
        this.hoveredCameraId = null;
    }

    update(cameras, visibility, hoveredCameraId = null) {
        this.hoveredCameraId = hoveredCameraId;

        // Clear existing meshes
        this.frustumMeshes.forEach((mesh, id) => {
            this.coverageGroup.remove(mesh);
            mesh.geometry.dispose();
            mesh.material.dispose();
        });
        this.frustumMeshes.clear();

        if (!visibility.frustums || !visibility.cameras) return;

        // Only show hovered camera's frustum, or all if explicitly toggled
        cameras.forEach(camera => {
            const shouldShow = hoveredCameraId === null || hoveredCameraId === camera.id;
            if (shouldShow) {
                const isHovered = hoveredCameraId === camera.id;
                const frustumMesh = this.createFrustumVolume(camera, isHovered);
                if (frustumMesh) {
                    this.frustumMeshes.set(camera.id, frustumMesh);
                    this.coverageGroup.add(frustumMesh);
                }
            }
        });
    }

    createFrustumVolume(camera, isHovered = false) {
        const fovH = camera.fov_h_deg * (Math.PI / 180);
        const fovV = camera.fov_v_deg * (Math.PI / 180);

        // Full size when hovered, small size otherwise
        const depth = isHovered ? COVERAGE_FRUSTUM_DEPTH_HOVER_M : COVERAGE_FRUSTUM_DEPTH_M;

        // Create frustum geometry as a pyramid
        const nearDist = 10.0;
        const farDist = depth;

        const nearHalfWidth = Math.tan(fovH / 2) * nearDist;
        const nearHalfHeight = Math.tan(fovV / 2) * nearDist;
        const farHalfWidth = Math.tan(fovH / 2) * farDist;
        const farHalfHeight = Math.tan(fovV / 2) * farDist;

        // Create vertices for frustum
        const vertices = new Float32Array([
            // Near plane (4 corners)
            -nearHalfWidth, -nearHalfHeight, nearDist,
            nearHalfWidth, -nearHalfHeight, nearDist,
            nearHalfWidth, nearHalfHeight, nearDist,
            -nearHalfWidth, nearHalfHeight, nearDist,
            // Far plane (4 corners)
            -farHalfWidth, -farHalfHeight, farDist,
            farHalfWidth, -farHalfHeight, farDist,
            farHalfWidth, farHalfHeight, farDist,
            -farHalfWidth, farHalfHeight, farDist,
        ]);

        // Indices for frustum faces (convex hull)
        const indices = new Uint16Array([
            // Far plane
            4, 5, 6,  4, 6, 7,
            // Bottom face
            0, 1, 5,  0, 5, 4,
            // Right face
            1, 2, 6,  1, 6, 5,
            // Top face
            2, 3, 7,  2, 7, 6,
            // Left face
            3, 0, 4,  3, 4, 7,
        ]);

        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.BufferAttribute(vertices, 3));
        geometry.setIndex(new THREE.BufferAttribute(indices, 1));
        geometry.computeVertexNormals();

        // Brighter and more opaque when hovered
        const opacity = isHovered ? COVERAGE_HOVER_OPACITY : COVERAGE_BASE_OPACITY;
        const color = isHovered ? 0x00ffff : 0x00aaff; // Brighter cyan on hover

        // Semi-transparent material with additive blending for overlap
        const material = new THREE.MeshBasicMaterial({
            color: color,
            transparent: true,
            opacity: opacity,
            side: THREE.DoubleSide,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
        });

        const mesh = new THREE.Mesh(geometry, material);

        // Position and orient the frustum
        const pos = camera.position;
        mesh.position.set(pos[0], pos[1], pos[2]);

        // Apply camera rotation
        if (camera.rotation_quat && camera.rotation_quat.length === 4) {
            mesh.quaternion.set(
                camera.rotation_quat[0],
                camera.rotation_quat[1],
                camera.rotation_quat[2],
                camera.rotation_quat[3]
            ).normalize();
        }

        mesh.userData.cameraId = camera.id;

        return mesh;
    }

    setVisible(visible) {
        this.coverageGroup.visible = visible;
    }
}
