// Voxel point cloud rendering with temporal decay
import * as THREE from 'three';

const VOXEL_POINT_SIZE_PX = 6;

export class VoxelRenderer {
    constructor(scene) {
        this.scene = scene;
        this.voxelGroup = new THREE.Group();
        this.voxelGroup.name = 'voxels';
        this.scene.add(this.voxelGroup);

        this.pointClouds = [];
    }

    update(weavefieldHistory, visibility) {
        // Clear existing point clouds
        this.pointClouds.forEach(pc => {
            this.voxelGroup.remove(pc);
            pc.geometry.dispose();
            pc.material.dispose();
        });
        this.pointClouds = [];

        if (!visibility.voxels || !weavefieldHistory || weavefieldHistory.length === 0) {
            return;
        }

        // Create point cloud for each weavefield volume
        weavefieldHistory.forEach((volume, index) => {
            if (volume.voxels && volume.voxels.length > 0) {
                const pointCloud = this.createPointCloud(volume, index, weavefieldHistory.length);
                this.pointClouds.push(pointCloud);
                this.voxelGroup.add(pointCloud);
            }
        });
    }

    createPointCloud(volume, index, totalVolumes) {
        const voxels = volume.voxels;
        const grid = volume.grid;

        // Calculate temporal decay (older = more transparent)
        const age = totalVolumes - index - 1;
        const decayFactor = Math.exp(-age * 0.1);

        // Create geometry
        const positions = [];
        const colors = [];
        const sizes = [];

        voxels.forEach(voxel => {
            // Convert voxel indices to voxel centers in world meters.
            const x = grid.origin[0] + (voxel.ix + 0.5) * grid.voxel_size_m;
            const y = grid.origin[1] + (voxel.iy + 0.5) * grid.voxel_size_m;
            const z = grid.origin[2] + (voxel.iz + 0.5) * grid.voxel_size_m;

            positions.push(x, y, z);

            // Color based on score and support count (multi-camera agreement)
            const normalizedScore = Math.min(1.0, voxel.score / 5.0);
            const supportCount = voxel.support_count || 2; // Default to minimum if not present
            const supportFactor = Math.min(1.5, supportCount / 2.0); // Boost brightness with more cameras
            const color = this.scoreToColor(normalizedScore, supportFactor);
            colors.push(color.r, color.g, color.b);

            // Size based on score
            const size = 0.1 + normalizedScore * 0.3;
            sizes.push(size);
        });

        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
        geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
        geometry.setAttribute('size', new THREE.Float32BufferAttribute(sizes, 1));

        // Create shader material for glowing points
        const material = new THREE.PointsMaterial({
            size: VOXEL_POINT_SIZE_PX,
            vertexColors: true,
            transparent: true,
            opacity: 0.8 * decayFactor,
            blending: THREE.AdditiveBlending,
            sizeAttenuation: false,
            depthWrite: false
        });

        return new THREE.Points(geometry, material);
    }

    scoreToColor(normalizedScore, supportFactor = 1.0) {
        // Gradient: dark gray (0) -> light gray (0.5) -> white (1.0)
        // supportFactor brightens colors when multiple cameras agree
        const color = new THREE.Color();

        if (normalizedScore < 0.5) {
            // Dark gray to medium gray
            const t = normalizedScore * 2;
            color.lerpColors(
                new THREE.Color(0x333333),
                new THREE.Color(0x888888),
                t
            );
        } else {
            // Medium gray to white
            const t = (normalizedScore - 0.5) * 2;
            color.lerpColors(
                new THREE.Color(0x888888),
                new THREE.Color(0xffffff),
                t
            );
        }

        // Boost brightness based on camera support
        color.r = Math.min(1.0, color.r * supportFactor);
        color.g = Math.min(1.0, color.g * supportFactor);
        color.b = Math.min(1.0, color.b * supportFactor);

        return color;
    }
}
