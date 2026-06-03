// Ray visualization from cameras to tracks
import * as THREE from 'three';

export class RayRenderer {
    constructor(scene) {
        this.scene = scene;
        this.rayGroup = new THREE.Group();
        this.rayGroup.name = 'rays';
        this.scene.add(this.rayGroup);

        this.rayLines = [];
    }

    update(tracks, cameras, rayMode) {
        // Clear existing rays
        this.rayLines.forEach(line => {
            this.rayGroup.remove(line);
            line.geometry.dispose();
            line.material.dispose();
        });
        this.rayLines = [];

        if (rayMode === 'none' || tracks.size === 0 || cameras.size === 0) {
            return;
        }

        // Create rays based on mode
        if (rayMode === 'all') {
            this.createAllRays(tracks, cameras);
        } else if (rayMode.startsWith('camera_')) {
            const cameraId = parseInt(rayMode.split('_')[1]);
            const camera = cameras.get(cameraId);
            if (camera) {
                this.createCameraRays(tracks, camera);
            }
        }
    }

    createAllRays(tracks, cameras) {
        const cameraArray = Array.from(cameras.values());

        tracks.forEach(track => {
            const trackPos = new THREE.Vector3(track.state[0], track.state[1], track.state[2]);

            cameraArray.forEach((camera, index) => {
                const cameraPos = new THREE.Vector3(camera.position[0], camera.position[1], camera.position[2]);

                // Create ray line
                const line = this.createRayLine(cameraPos, trackPos, index);
                this.rayLines.push(line);
                this.rayGroup.add(line);
            });
        });
    }

    createCameraRays(tracks, camera) {
        const cameraPos = new THREE.Vector3(camera.position[0], camera.position[1], camera.position[2]);

        tracks.forEach(track => {
            const trackPos = new THREE.Vector3(track.state[0], track.state[1], track.state[2]);

            // Create ray line
            const line = this.createRayLine(cameraPos, trackPos, camera.id);
            this.rayLines.push(line);
            this.rayGroup.add(line);
        });
    }

    createRayLine(start, end, cameraIndex) {
        const points = [start, end];
        const geometry = new THREE.BufferGeometry().setFromPoints(points);

        // Subtle white rays for all cameras
        const colors = [0xffffff, 0xcccccc, 0xaaaaaa, 0x999999, 0x888888];
        const color = colors[cameraIndex % colors.length];

        const material = new THREE.LineBasicMaterial({
            color: color,
            transparent: true,
            opacity: 0.25,
            linewidth: 1
        });

        return new THREE.Line(geometry, material);
    }
}
